"""Regressions for execution evidence, handoff ownership and durable results."""
import asyncio
import html
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .common import settings_at
    from .test_bot import FakeAPI, FakeRunner, attachment_update, update
except ImportError:
    from common import settings_at
    from test_bot import FakeAPI, FakeRunner, attachment_update, update
from agy_runner import Result
from bot import Bridge, Job, html_chunks
from state_store import Store
from telegram_api import TelegramError


class ExecutionEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed,
                           self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        self.runner.finish.set()
        if self.bridge.slot is not None:
            self.bridge.slot.cancel.set()
            if self.bridge.slot.worker is not None:
                await asyncio.wait_for(self.bridge.slot.worker, 2)
        if self.bridge.steer_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(self.bridge.steer_tasks),
                                                return_exceptions=True), 2)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

    async def test_capacity_fallback_never_replays_after_active_or_evicted_tool(self):
        for activity in ("正在调用工具：change_file", "✅ 💻 执行命令 「change-file」"):
            with self.subTest(activity=activity):
                calls = []
                async def run(prompt, cancel, **kwargs):
                    calls.append(prompt)
                    await kwargs["progress"](activity)
                    for index in range(8):
                        await kwargs["progress"](f"✅ 🧠 规划步骤完成 {index}")
                    return Result("error", category="capacity", detail="No capacity available")
                self.runner.run = run
                self.store.set_model(12345, "gemini-3.8-flash-low")
                await self.bridge.handle(update("perform one operation"))
                job = self.bridge.slot
                await job.worker
                self.assertEqual(calls, ["perform one operation"])
                self.assertTrue(job.tool_started)
                self.assertFalse(any("💻" in step or "🛠️" in step for step in job.execution_steps))
                self.assertEqual(self.store.get_model(12345), "gemini-3.8-flash-low")
                self.assertEqual(self.store.load(12345)["outcome"], "error")

    async def _gated_handoff(self):
        self.runner.hold = True
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bridge._restart_with_correction
        async def gated(job, correction):
            await job.worker
            entered.set()
            await release.wait()
            await original(job, correction)
        self.bridge._restart_with_correction = gated
        await self.bridge.handle(update("old operation"))
        old = self.bridge.slot
        await self.runner.started.wait()
        await self.bridge.handle(update("/steer corrected operation"))
        await asyncio.wait_for(entered.wait(), 2)
        return old, release

    async def test_handoff_reserves_slot_against_text_album_and_model_refresh(self):
        old, release = await self._gated_handoff()
        try:
            self.assertTrue(old.worker.done())
            self.assertIs(self.bridge.slot, old)
            await self.bridge.handle(update("independent operation", user=67890))
            await self.bridge.handle(attachment_update(user=67890, media_group_id="album",
                                     document={"file_id": "file", "file_size": 4, "file_name": "a.log"}))
            await self.bridge.handle(update("/model refresh", user=67890))
            self.assertIs(self.bridge.slot, old)
            self.assertIsNone(self.bridge.pending_album)
            self.assertIsNone(self.bridge.model_check_task)
            self.assertEqual(self.runner.calls, 1)
        finally:
            release.set()
            await old.steer_task
        corrected = self.bridge.slot
        self.assertIsNot(corrected, old)
        self.runner.finish.set()
        await corrected.worker
        self.assertEqual(self.runner.calls, 2)

    async def test_cancel_during_handoff_cancels_replacement(self):
        old, release = await self._gated_handoff()
        try:
            await self.bridge.handle(update("/cancel"))
        finally:
            release.set()
            await old.steer_task
        self.assertTrue(old.steer_cancelled)
        self.assertIsNone(self.bridge.slot)
        self.assertEqual(self.runner.calls, 1)

    async def test_handoff_does_not_overwrite_another_job(self):
        old = Job(12345, 12345, original_prompt="old")
        other = Job(67890, 67890)
        self.bridge.slot = other
        await self.bridge._restart_with_correction(old, "correction")
        self.assertIs(self.bridge.slot, other)
        self.assertEqual(self.runner.calls, 0)

    async def test_oversized_correction_releases_reserved_slot(self):
        old = Job(12345, 12345, original_prompt="a" * self.settings.max_prompt)
        self.bridge.slot = old
        await self.bridge._restart_with_correction(old, "more text")
        self.assertIsNone(self.bridge.slot)
        self.assertEqual(self.runner.calls, 0)

    async def test_result_is_durable_before_usage_failure_and_remains_retrievable(self):
        self.runner.result = Result("success", text="valuable final result", input_tokens=5, total_tokens=5)
        def fail_usage(*args):
            self.assertEqual(self.store.load(12345)["text"], "valuable final result")
            raise OSError("PRIVATE_USAGE_PATH")
        with patch.object(self.store, "record_usage", side_effect=fail_usage):
            await self.bridge.handle(update("one operation"))
            job = self.bridge.slot
            await job.worker
        record = self.store.load(12345)
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["delivery"], "sent")
        self.assertIn("用量", record["metadata_warning"])
        self.assertNotIn("PRIVATE_USAGE_PATH", str(record))
        self.assertFalse(self.runner.blocked)
        self.assertEqual(self.store.history_item(12345, job.job_id)["text"], "valuable final result")
        self.assertEqual(self.runner.calls, 1)
        await self.bridge.handle(update("/last"))
        await self.bridge.reply_worker
        self.assertIn("valuable final result", self.api.messages[-1][1])
        self.assertEqual(self.runner.calls, 1)

    async def test_conversation_failure_preserves_result_and_blocks_unsafe_followup(self):
        self.runner.result = Result("success", text="completed", conversation_id="new-conversation")
        def fail_context(*args):
            self.assertEqual(self.store.load(12345)["text"], "completed")
            raise ValueError("PRIVATE_CONTEXT")
        with patch.object(self.store, "set_conversation", side_effect=fail_context):
            await self.bridge.handle(update("one operation"))
            await self.bridge.slot.worker
        record = self.store.load(12345)
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["delivery"], "sent")
        self.assertIn("会话", record["metadata_warning"])
        self.assertNotIn("PRIVATE_CONTEXT", str(record))
        self.assertFalse(self.runner.blocked)
        await self.bridge.handle(update("new task"))
        await self.bridge.slot.worker
        self.assertEqual(self.runner.calls, 2)

    async def test_history_failure_cannot_erase_already_completed_result(self):
        self.runner.result = Result("success", text="completed before history failure")
        with patch.object(self.store, "save_history", side_effect=OSError("history failure")):
            await self.bridge.handle(update("operation"))
            await self.bridge.slot.worker
        self.assertEqual(self.store.load(12345)["text"], "completed before history failure")
        self.assertFalse(self.runner.blocked)
        self.assertEqual(self.runner.calls, 1)

    async def test_html_second_page_downgrade_never_repeats_first_page(self):
        text = "<b>UNIQUE_START|" + "A" * 4000 + "&amp;MIDDLE|" + "B" * 4000 + "|END</b>"
        chunks = html_chunks(text)
        accepted, attempts = [], []
        async def send(chat, body, reply_markup=None, parse_mode=None):
            attempts.append(parse_mode)
            if len(attempts) == 2:
                raise TelegramError(400)
            accepted.append(body)
            return {"message_id": len(attempts)}
        self.api.send = send
        self.assertTrue(await self.bridge.send_html(12345, text))
        self.assertEqual(len(accepted), len(chunks))
        self.assertEqual(sum(part.count("UNIQUE_START|") for part in accepted), 1)
        expected = html.unescape(re.sub(r"<[^>]+>", "", chunks[1]))
        self.assertEqual(accepted[1], expected + f"\n\n📄 [第 2/{len(chunks)} 页]")
        self.assertEqual(attempts[:3], ["HTML", "HTML", None])
        self.assertEqual(attempts[3:], ["HTML"] * (len(chunks) - 2))

    async def test_html_ambiguous_second_page_failure_is_not_retried(self):
        attempts = []
        async def send(chat, body, **kwargs):
            attempts.append(body)
            if len(attempts) == 2:
                raise TelegramError()
            return {"message_id": len(attempts)}
        self.api.send = send
        self.assertFalse(await self.bridge.send_html(12345, "X" * 9000))
        self.assertEqual(len(attempts), 2)

    async def test_html_failed_plain_page_stops_remaining_pages(self):
        attempts = []
        async def send(chat, body, **kwargs):
            attempts.append(body)
            if len(attempts) == 2:
                raise TelegramError(400)
            if len(attempts) == 3:
                raise TelegramError()
            return {"message_id": len(attempts)}
        self.api.send = send
        self.assertFalse(await self.bridge.send_html(12345, "X" * 9000))
        self.assertEqual(len(attempts), 3)

    async def test_interrupted_task_survives_replacement_of_last_result(self):
        record = self.store.save(12345, {"job_id": "a" * 12, "outcome": "running",
                                         "title": "important task\nwith title"})
        self.store.recover_interrupted()
        recovered = self.store.history_item(12345, record["job_id"])
        self.assertEqual(recovered["outcome"], "interrupted")
        self.assertEqual(recovered["title"], "important task with title")
        self.store.save(12345, {"job_id": "b" * 12, "outcome": "success", "text": "new result"})
        self.store.recover_interrupted()
        self.assertEqual(self.store.history_item(12345, record["job_id"])["outcome"], "interrupted")
        self.assertEqual(len([item for item in self.store.history(12345)
                              if item["job_id"] == record["job_id"]]), 1)
        self.assertEqual(self.runner.calls, 0)

    async def test_interruption_recovery_retries_only_missing_history_write(self):
        self.store.save(12345, {"job_id": "a" * 12, "outcome": "running"})
        with patch.object(self.store, "save_history", side_effect=OSError("history failure")):
            with self.assertRaises(OSError):
                self.store.recover_interrupted()
        self.assertEqual(self.store.load(12345)["outcome"], "interrupted")
        self.store.recover_interrupted()
        self.store.recover_interrupted()
        history = self.store.history(12345)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["title"], "服务中断的任务")
        self.assertEqual(history[0]["outcome"], "interrupted")
        self.assertEqual(self.runner.calls, 0)

    async def test_running_record_keeps_bounded_redacted_title_for_recovery(self):
        self.runner.hold = True
        await self.bridge.handle(update("recoverable title"))
        job = self.bridge.slot
        self.assertEqual(self.store.load(12345)["title"], "recoverable title")
        job.cancel.set()
        await job.worker
        record = self.store.save(12345, {"title": self.settings.token + "Z" * 300,
                                         "metadata_warning": self.settings.token + "W" * 700})
        self.assertLessEqual(len(record["title"]), 160)
        self.assertLessEqual(len(record["metadata_warning"]), 500)
        self.assertNotIn(self.settings.token, str(record))
