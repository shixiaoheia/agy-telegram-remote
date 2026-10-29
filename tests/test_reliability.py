"""Regressions for control responsiveness, progress recovery and stored results."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    from .common import settings_at
    from .test_bot import FakeAPI, FakeRunner, attachment_update, callback_update, update
except ImportError:
    from common import settings_at
    from test_bot import FakeAPI, FakeRunner, attachment_update, callback_update, update
from bot import Bridge, Job, deployment_version
from agy_runner import Result
from settings import OFFICIAL_MODELS, is_retired_model, resolve_model
from state_store import Store
from telegram_api import TelegramError


REMOVED = (
    "gpt-oss-120b-medium", "claude-opus-4-6-thinking", "claude-sonnet-4-6",
    "gemini-3.7-flash-high", "gemini-3.6-flash-low",
)


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed,
                           self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        if self.bridge.model_check_task is not None:
            task = self.bridge.model_check_task
            self.bridge._cancel_model_health()
            await asyncio.gather(task, return_exceptions=True)
        if self.bridge.restart_task is not None:
            self.bridge.restart_task.cancel()
            await asyncio.gather(self.bridge.restart_task, return_exceptions=True)
        self.runner.finish.set()
        if self.bridge.slot:
            self.bridge.slot.cancel.set()
            if self.bridge.slot.worker:
                await asyncio.wait_for(self.bridge.slot.worker, 2)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

    async def test_internal_runner_typeerror_is_never_retried(self):
        self.runner.run = AsyncMock(side_effect=TypeError("internal failure after entry"))
        with self.assertRaises(TypeError):
            await self.bridge._run_task(Job(12345, 12345), "one execution only")
        self.runner.run.assert_awaited_once()

    async def test_waiting_tasks_run_in_order_only_after_slot_release(self):
        self.runner.hold = True
        prompts = []
        run = self.runner.run
        async def record(prompt, *args, **kwargs):
            prompts.append(prompt)
            return await run(prompt, *args, **kwargs)
        self.runner.run = record
        await self.bridge.handle(update("first"))
        await self.runner.started.wait()
        first = self.bridge.slot
        await self.bridge.handle(update("/queue add second"))
        await self.bridge.handle(update("third", user=67890))
        self.assertEqual(prompts, ["first"])
        self.assertEqual(len(self.bridge.waiting_jobs), 2)
        self.runner.finish.set()
        await first.worker
        for _ in range(100):
            if len(prompts) == 3:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(prompts, ["first", "second", "third"])
        if self.bridge.slot:
            await self.bridge.slot.worker

    async def test_queue_limit_and_user_cancel_leave_other_users_entries(self):
        self.runner.hold = True
        await self.bridge.handle(update("first"))
        await self.runner.started.wait()
        await self.bridge.handle(update("/queue add second"))
        await self.bridge.handle(update("third", user=67890))
        await self.bridge.handle(update("/queue add fourth"))
        await self.bridge.handle(update("fifth", user=67890))
        self.assertEqual(len(self.bridge.waiting_jobs), 3)
        await self.bridge.handle(update("/queue cancel"))
        self.assertEqual([item[0] for item in self.bridge.waiting_jobs], [67890])
        self.bridge.waiting_jobs.clear()

    async def test_slow_callback_answer_does_not_block_next_update(self):
        gate = asyncio.Event()
        entered = asyncio.Event()
        async def slow_answer(*args, **kwargs):
            entered.set()
            await gate.wait()
        self.api.answer_callback_query = slow_answer
        await asyncio.wait_for(self.bridge.consume_updates([
            callback_update("progress:expand:expired"),
            update("/status", update_id=2),
        ]), 1)
        await entered.wait()
        self.assertFalse(gate.is_set())
        await self.bridge.reply_worker
        self.assertTrue(self.api.messages)
        gate.set()
        await asyncio.gather(*tuple(self.bridge.callback_tasks))

    async def test_worker_typeerror_does_not_replay_or_block_new_tasks(self):
        effects = []
        async def fail(*args, **kwargs):
            effects.append("executed")
            raise TypeError("private failure")
        self.runner.run = fail
        await self.bridge.handle(update("task"))
        await self.bridge.slot.worker
        self.assertEqual(effects, ["executed"])
        self.assertFalse(self.runner.blocked)
        await self.bridge.handle(update("another task"))
        await self.bridge.slot.worker
        self.assertEqual(effects, ["executed", "executed"])

    async def test_model_cancel_waits_for_cleanup_and_preserves_full_cache(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.runner.list_models = AsyncMock(return_value=("gemini-3.8-flash-high", "gemini-3.1-pro-high"))
        self.store.set_model_health({"existing": {"outcome": "success"}})
        before = self.store.get_model_health()
        async def probe(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        self.runner.run = AsyncMock(side_effect=probe)
        await self.bridge.handle(update("/model refresh"))
        task = self.bridge.model_check_task
        await started.wait()
        try:
            await self.bridge.handle(update("/status"))
            await self.bridge.reply_worker
            self.assertIn("1/2：gemini-3.8-flash-high", self.api.messages[-1][1])
            await self.bridge.handle(update("/cancel", user=67890))
            self.assertFalse(self.bridge.model_check_cancelling)
            await self.bridge.handle(update("/cancel"))
            await cleaning.wait()
            await self.bridge.handle(update("/cancel"))
            await self.bridge.handle(update("new task"))
            await self.bridge.handle(update("/restart"))
            self.assertIsNone(self.bridge.slot)
            self.assertIsNone(self.bridge.restart_task)
            self.assertFalse(task.done())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(self.bridge.model_check_task)
        self.runner.run.assert_awaited_once()
        self.assertEqual(self.store.get_model_health(), before)

    async def test_cancel_model_discovery_before_any_probe(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def discover():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.runner.list_models = discover
        await self.bridge.handle(update("/model refresh"))
        task = self.bridge.model_check_task
        await entered.wait()
        await self.bridge.handle(update("/cancel"))
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.runner.calls, 0)
        self.assertIsNone(self.bridge.model_check_task)

    async def test_immediate_model_cancel_clears_reservation(self):
        await self.bridge.handle(update("/model refresh"))
        task = self.bridge.model_check_task
        await self.bridge.handle(update("/cancel"))
        await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(self.bridge.model_check_task)
        self.assertIsNone(self.bridge.model_check_user)
        self.assertFalse(self.bridge.model_check_cancelling)
        self.assertEqual(self.runner.calls, 0)

    async def test_shutdown_awaits_already_cancelled_model_cleanup(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def discover():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        self.runner.list_models = discover
        await self.bridge.handle(update("/model refresh"))
        check = self.bridge.model_check_task
        await entered.wait()
        await self.bridge.handle(update("/cancel"))
        await cleaning.wait()
        self.bridge.stop.set()
        with patch.object(self.bridge, "initialize_with_retry", new=AsyncMock()):
            shutdown = asyncio.create_task(self.bridge.run())
            try:
                await asyncio.sleep(0)
                self.assertFalse(shutdown.done())
                self.assertFalse(check.done())
            finally:
                release.set()
                await shutdown
        self.assertTrue(check.done())
        self.assertIsNone(self.bridge.model_check_task)

    async def test_shutdown_cancels_active_model_probe(self):
        self.runner.hold = True
        await self.bridge.handle(update("/model refresh"))
        check = self.bridge.model_check_task
        await self.runner.started.wait()
        self.bridge.stop.set()
        with patch.object(self.bridge, "initialize_with_retry", new=AsyncMock()):
            await asyncio.wait_for(self.bridge.run(), 2)
        self.assertTrue(check.done())
        self.assertIsNone(self.bridge.model_check_task)
        self.assertEqual(self.runner.calls, 1)

    async def test_corrupt_history_button_preserves_file_and_later_updates(self):
        path = self.settings.state_dir / "history-12345.json"
        damaged = b'{"items": broken'
        path.write_bytes(damaged)
        await self.bridge.consume_updates([
            callback_update("history:" + "a" * 12), update("/status", update_id=2),
        ])
        await self.bridge.reply_worker
        self.assertEqual(path.read_bytes(), damaged)
        self.assertEqual(self.bridge.offset, 3)
        self.assertEqual(self.runner.calls, 0)
        self.assertTrue(any("无法读取任务历史" in text for _, text, _ in self.api.messages))
        self.assertTrue(any("部署版本" in text for _, text, _ in self.api.messages))
        self.assertTrue(self.api.answered_callbacks)

    async def test_history_button_permission_error_is_not_exposed(self):
        with patch.object(self.store, "history_item", side_effect=PermissionError("PRIVATE_PATH")):
            await self.bridge.handle(callback_update("history:" + "a" * 12))
        await self.bridge.reply_worker
        self.assertIn("原记录已保留", self.api.messages[-1][1])
        self.assertNotIn("PRIVATE_PATH", self.api.messages[-1][1])
        self.assertEqual(self.runner.calls, 0)

    async def test_deployment_version_accepts_only_full_commit_marker(self):
        directory = Path(self.temp.name)
        marker = directory / ".commit"
        marker.write_text("A" * 40 + "\n", encoding="ascii")
        self.assertEqual(deployment_version(directory), "a" * 12)
        for content in ("short", "x" * 40, "a" * 43, "凭据", "a" * 40 + "\nSECRET"):
            marker.write_text(content, encoding="utf-8")
            self.assertIn("未知", deployment_version(directory))

    async def test_deployment_version_missing_link_and_fifo_are_unknown(self):
        directory = Path(self.temp.name)
        marker = directory / ".commit"
        self.assertIn("未知", deployment_version(directory))
        target = directory / "external"
        target.write_text("b" * 40, encoding="ascii")
        marker.symlink_to(target)
        self.assertIn("未知", deployment_version(directory))
        marker.unlink()
        os.mkfifo(marker)
        self.assertIn("未知", deployment_version(directory))

    async def test_status_uses_cached_running_version_and_start_time(self):
        with patch("bot.deployment_version", return_value="123456789abc"):
            bridge = Bridge(self.settings, self.api, self.store, self.runner)
        bridge._started_at = 1700000000
        with patch("bot.deployment_version", side_effect=AssertionError("must not reread marker")):
            await bridge.handle(update("/status details"))
        await bridge.reply_worker
        text = self.api.messages[-1][1]
        self.assertIn("部署版本：123456789abc", text)
        self.assertIn("服务启动：", text)
        self.assertIn("服务器时间", text)
        self.assertEqual(self.runner.calls, 0)

    async def test_large_chinese_results_do_not_block_later_tasks(self):
        self.runner.result = Result("success", text="中" * 30000)
        for index in range(6):
            await self.bridge.handle(update(f"task {index}"))
            await self.bridge.slot.worker
            self.assertFalse(self.runner.blocked)
        self.assertEqual(self.runner.calls, 6)
        history = self.store.history(12345)
        self.assertEqual(len(history), 6)
        self.assertTrue(all(item["delivery"] == "sent" and item["text"] == "中" * 30000
                            for item in history))

    async def test_restart_waits_until_cancelled_worker_cleanup_is_finished(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(_prompt, cancel, **kwargs):
            started.set()
            await cancel.wait()
            cleaning.set()
            await release.wait()
            return Result("cancelled")

        self.runner.run = run
        with patch("bot.os._exit") as execute, patch("bot.RESTART_DELAY_SECONDS", 0):
            await self.bridge.handle(update("safe task"))
            await asyncio.wait_for(started.wait(), 1)
            job = self.bridge.slot
            try:
                await self.bridge.handle(update("/cancel"))
                await asyncio.wait_for(cleaning.wait(), 1)
                await self.bridge.handle(update("/restart"))
                self.assertIsNone(self.bridge.restart_task)
                self.assertIs(self.bridge.slot, job)
                execute.assert_not_called()
            finally:
                release.set()
                await job.worker
            await self.bridge.handle(update("/restart"))
            restart = self.bridge.restart_task
            self.assertIsNotNone(restart)
            await restart
            execute.assert_called_once()

    async def test_restart_remains_available_after_failed_cleanup(self):
        self.runner.blocked = True
        with patch("bot.os._exit") as execute, patch("bot.RESTART_DELAY_SECONDS", 0):
            await self.bridge.handle(update("/restart"))
            self.assertIsNotNone(self.bridge.restart_task)
            await self.bridge.restart_task
            execute.assert_called_once_with(75)
        self.runner.blocked = False

    async def test_restart_rejects_album_model_probe_and_pending_correction(self):
        waiting = asyncio.create_task(asyncio.Event().wait())
        try:
            with patch("bot.os._exit") as execute:
                for field, value in (("pending_album", {"user": 12345}),
                                     ("album_task", waiting), ("model_check_task", waiting)):
                    setattr(self.bridge, field, value)
                    await self.bridge.handle(update("/restart"))
                    self.assertIsNone(self.bridge.restart_task)
                    setattr(self.bridge, field, None)
                self.bridge.steer_tasks.add(waiting)
                await self.bridge.handle(update("/restart"))
                self.assertIsNone(self.bridge.restart_task)
                self.bridge.steer_tasks.clear()
                execute.assert_not_called()
        finally:
            self.runner.blocked = False
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_restart_reservation_blocks_new_work_and_duplicate_restarts(self):
        with patch("bot.os._exit") as execute, patch("bot.RESTART_DELAY_SECONDS", 0.02):
            await self.bridge.handle(update("/restart"))
            restart = self.bridge.restart_task
            await self.bridge.handle(update("a new task"))
            await self.bridge.handle(attachment_update(
                photo=[{"file_id": "photo", "file_size": 4}], media_group_id="album",
            ))
            await self.bridge.handle(update("/model refresh"))
            await self.bridge.handle(update("/restart"))
            self.assertIs(self.bridge.restart_task, restart)
            self.assertIsNone(self.bridge.slot)
            self.assertIsNone(self.bridge.pending_album)
            self.assertIsNone(self.bridge.model_check_task)
            self.assertEqual(self.runner.calls, 0)
            await restart
            execute.assert_called_once()
            self.assertFalse(self.bridge._restart_pending())

    async def test_restart_checks_for_new_busy_state_immediately_before_exec(self):
        with patch("bot.os._exit") as execute, patch("bot.RESTART_DELAY_SECONDS", 0):
            await self.bridge.handle(update("/restart"))
            restart = self.bridge.restart_task
            self.bridge.slot = Job(12345, 12345)
            self.bridge.slot.cancel.set()
            try:
                await restart
                execute.assert_not_called()
                self.assertFalse(self.bridge._restart_pending())
            finally:
                self.bridge.slot = None

    async def test_shutdown_cancels_pending_restart_without_exec(self):
        with patch("bot.os._exit") as execute:
            await self.bridge.handle(update("/restart"))
            restart = self.bridge.restart_task
            self.bridge.stop.set()
            await asyncio.wait_for(self.bridge.run(), 1)
            self.assertTrue(restart.done())
            self.assertFalse(self.bridge._restart_pending())
            execute.assert_not_called()

    async def test_completion_buttons_retrieve_history_and_start_new_conversation_without_execution(self):
        await self.bridge.handle(update("first task"))
        job = self.bridge.slot
        await job.worker
        card = self.api.messages[self.api.message_indices[job.status_message_id]]
        self.assertIn("the result", card[1])
        buttons = [b["callback_data"] for row in card[2]["inline_keyboard"] for b in row]
        self.assertEqual(buttons, [f"history:{job.job_id}", "tasks:history", "tasks:new"])
        await self.bridge.handle(callback_update(buttons[0]))
        await self.bridge.reply_worker
        self.assertIn("the result", self.api.messages[-1][1])
        await self.bridge.handle(callback_update("tasks:history"))
        await self.bridge.reply_worker
        self.assertIn("first task", self.api.messages[-1][1])
        self.store.set_conversation(12345, "existing-conversation")
        await self.bridge.handle(callback_update("tasks:new"))
        await self.bridge.reply_worker
        self.assertIsNotNone(self.store.get_conversation(12345))
        action = self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]
        await self.bridge.handle(callback_update(action))
        await self.bridge.reply_worker
        self.assertIsNone(self.store.get_conversation(12345))
        self.assertEqual(self.runner.calls, 1)

    async def test_old_completion_button_returns_its_own_result(self):
        self.runner.result = Result("success", text="first saved result")
        await self.bridge.handle(update("first task"))
        first = self.bridge.slot
        await first.worker
        self.runner.result = Result("success", text="second saved result")
        await self.bridge.handle(update("second task"))
        await self.bridge.slot.worker
        await self.bridge.handle(callback_update(f"history:{first.job_id}"))
        await self.bridge.reply_worker
        self.assertIn("first saved result", self.api.messages[-1][1])
        self.assertNotIn("second saved result", self.api.messages[-1][1])
        self.assertEqual(self.runner.calls, 2)

    async def test_completion_buttons_cannot_access_another_user_or_group(self):
        record = self.store.save(12345, {"job_id": "a" * 12, "outcome": "success", "text": "private result"})
        self.store.save_history(12345, "private task", record)
        self.store.set_conversation(12345, "private-conversation")
        await self.bridge.handle(callback_update("history:" + "a" * 12, user=67890, chat_id=67890))
        for action in ("tasks:history", "tasks:new", "history:" + "a" * 12):
            await self.bridge.handle(callback_update(action, user=98765, chat_id=98765))
            event = callback_update(action)
            event["callback_query"]["message"]["chat"]["type"] = "group"
            await self.bridge.handle(event)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.assertFalse(any("private result" in text for _, text, _ in self.api.messages))
        self.assertEqual(self.store.get_conversation(12345)["conversation_id"], "private-conversation")
        self.assertEqual(self.runner.calls, 0)

    async def test_new_conversation_button_during_execution_defers_reset(self):
        self.store.set_conversation(12345, "old-conversation")
        self.runner.hold = True
        self.runner.result = Result("success", text="done", conversation_id="old-conversation")
        await self.bridge.handle(update("safe task"))
        await self.runner.started.wait()
        job = self.bridge.slot
        await self.bridge.handle(callback_update("tasks:new"))
        self.assertFalse(job.reset_after)
        await self.bridge.reply_worker
        action = self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]
        await self.bridge.handle(callback_update(action))
        self.assertTrue(job.reset_after)
        self.assertIsNotNone(self.store.get_conversation(12345))
        self.runner.finish.set()
        await job.worker
        self.assertIsNone(self.store.get_conversation(12345))
        self.assertEqual(self.runner.calls, 1)

    async def test_delivery_failure_updates_card_and_retrieval_does_not_rerun(self):
        self.api.fail_html_edits = True
        await self.bridge.handle(update("safe task"))
        job = self.bridge.slot
        await job.worker
        card = self.api.messages[self.api.message_indices[job.status_message_id]]
        self.assertIn("任务完成", card[1])
        self.assertIn("结果发送失败或不完整", card[1])
        self.assertIn("不会重新执行任务", card[1])
        self.assertNotIn("下一条消息", card[1])
        self.assertEqual(self.store.history_item(12345, job.job_id)["delivery"], "failed_or_partial")
        await self.bridge.handle(callback_update(f"history:{job.job_id}"))
        await self.bridge.reply_worker
        self.assertIn("the result", self.api.messages[-1][1])
        self.assertEqual(self.runner.calls, 1)

    async def test_error_result_delivered_does_not_claim_execution_success(self):
        self.runner.result = Result("error", detail="fixture error")
        await self.bridge.handle(update("safe task"))
        job = self.bridge.slot
        await job.worker
        card = self.api.messages[self.api.message_indices[job.status_message_id]]
        self.assertIn("执行异常", card[1])
        self.assertIn("fixture error", card[1])
        self.assertEqual(self.store.load(12345)["delivery"], "sent")
        self.assertNotIn("任务完成", card[1])

    async def test_failed_result_and_card_edit_send_short_retrieval_notice(self):
        self.api.edit = AsyncMock(side_effect=TelegramError())
        await self.bridge.handle(update("safe task"))
        job = self.bridge.slot
        await job.worker
        self.assertIn("结果发送失败或不完整", self.api.messages[-1][1])
        buttons = self.api.messages[-1][2]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["callback_data"], f"history:{job.job_id}")
        self.assertEqual(self.api.sent_count, 2)
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.store.load(12345)["delivery"], "failed_or_partial")

    async def test_chinese_attachment_names_are_visible_while_disk_names_stay_safe(self):
        document = {"file_id": "chinese_log", "file_size": 4, "file_name": "运行日志.log"}
        await self.bridge.handle(attachment_update(document=document, caption="分析日志"))
        job = self.bridge.slot
        await job.worker
        self.assertIn("运行日志.log", self.api.sent_messages[0][1])
        self.assertIn("运行日志.log", self.runner.last_prompt)
        self.assertTrue(job.attachments[0].name.isascii())
        self.assertFalse(job.attachment_dir.exists())

    async def test_chinese_display_names_strip_paths_controls_and_keep_collisions_distinct(self):
        job = Job(12345, 12345)
        try:
            paths = []
            for name in ("../目录\\运行\u202e日志\n.log", "错误日志.log"):
                path, display = await self.bridge._receive_attachment(
                    job, {"file_id": "fixture_log", "file_size": 4, "file_name": name}, False,
                )
                self.assertNotIn("目录", display)
                self.assertNotIn("/", display)
                self.assertNotIn("\\", display)
                self.assertNotIn("\n", display)
                self.assertNotIn("\u202e", display)
                self.assertIn("日志.log", display)
                self.assertTrue(path.name.isascii())
                self.assertEqual(path.parent, self.settings.workspace / ".agy-telegram-inputs" / job.job_id)
                paths.append(path)
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all(p.read_bytes() == b"test" for p in paths))
        finally:
            self.bridge._cleanup_attachments(job)

    async def test_slow_download_does_not_block_status_or_cancel(self):
        started = asyncio.Event()

        async def download(_path, destination, maximum, *, cancel):
            destination.write_bytes(b"partial")
            started.set()
            await cancel.wait()
            destination.unlink()
            raise TelegramError()

        self.api.download_file = download
        doc = {"file_id": "file_1", "file_size": 4, "file_name": "slow.log"}
        await asyncio.wait_for(self.bridge.consume_updates([attachment_update(document=doc)]), 0.5)
        await asyncio.wait_for(started.wait(), 0.5)
        job = self.bridge.slot
        await asyncio.wait_for(self.bridge.consume_updates([
            update("/status", update_id=2), update("/cancel", update_id=3),
        ]), 0.5)
        self.assertTrue(job.cancel.is_set())
        await asyncio.wait_for(job.worker, 1)
        await self.bridge.reply_worker
        self.assertTrue(any("当前任务" in text for _, text, _ in self.api.messages))
        self.assertEqual(self.runner.calls, 0)
        self.assertIsNone(self.bridge.slot)
        self.assertFalse(job.attachment_dir.exists())
        self.assertEqual(self.store.load(12345)["outcome"], "cancelled")

    async def test_album_cancel_cleans_completed_and_partial_files_before_freeing_slot(self):
        started, release = asyncio.Event(), asyncio.Event()
        destinations = []

        async def download(_path, destination, maximum, *, cancel):
            destinations.append(destination)
            destination.write_bytes(b"test")
            if len(destinations) == 2:
                started.set()
                await cancel.wait()
                await release.wait()
                destination.unlink()
                raise TelegramError()
            return 4

        self.api.download_file = download
        items = [({"file_id": f"file_{i}", "file_size": 4, "file_name": f"{i}.log"}, False)
                 for i in range(3)]
        await self.bridge.handle(attachment_update(document=items[0][0]), album_items=items)
        await asyncio.wait_for(started.wait(), 0.5)
        job = self.bridge.slot
        await self.bridge.consume_updates([update("/cancel", update_id=2)])
        await self.bridge.consume_updates([update("another task", update_id=3)])
        self.assertIs(self.bridge.slot, job)
        self.assertEqual(self.runner.calls, 0)
        release.set()
        await asyncio.wait_for(job.worker, 1)
        self.assertEqual(len(destinations), 2)
        self.assertTrue(all(not p.exists() for p in destinations))
        self.assertFalse(job.attachment_dir.exists())
        self.assertIsNone(self.bridge.slot)

    async def test_progress_recovers_from_network_and_server_errors(self):
        job = Job(12345, 12345, status_message_id=1)
        self.bridge.slot = job
        job.progress_requested.set()

        # End only after the recovered edit, without introducing real timer waits.
        calls = 0

        async def edit(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TelegramError(0 if calls == 1 else 503)
            job.cancel.set()

        self.api.edit = AsyncMock(side_effect=edit)
        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.bridge._refresh_progress(job)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [10, 20])
        self.assertEqual(self.api.edit.await_count, 3)
        self.assertEqual(self.runner.calls, 0)

    async def test_progress_respects_429_and_stops_for_deleted_card(self):
        job = Job(12345, 12345, status_message_id=1)
        self.bridge.slot = job
        job.progress_requested.set()
        self.api.edit = AsyncMock(side_effect=[TelegramError(429, 17), TelegramError(400)])
        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.bridge._refresh_progress(job)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [17])
        self.assertEqual(self.api.edit.await_count, 2)

    async def test_stopping_progress_waits_for_inflight_edit(self):
        job = Job(12345, 12345, status_message_id=1)
        self.bridge.slot = job
        entered, release = asyncio.Event(), asyncio.Event()

        async def edit(*args, **kwargs):
            entered.set()
            await release.wait()

        self.api.edit = AsyncMock(side_effect=edit)
        with patch("bot.PROGRESS_UPDATE_SECONDS", 0):
            task = asyncio.create_task(self.bridge._refresh_progress(job))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(self.api.edit.await_count, 1)

    async def test_last_and_history_keep_settings_and_final_delivery(self):
        self.store.set_model(12345, "custom-model")
        self.store.set_effort(12345, "high")
        self.store.set_mode(12345, "plan")
        for delivery in ("sent", "failed_or_partial"):
            if delivery != "sent":
                self.api.fail_html_edits = True
            await self.bridge.handle(update("check the project"))
            await self.bridge.slot.worker
            last = self.store.load(12345)
            history = self.store.history_item(12345, last["job_id"])
            self.assertEqual(last["effort"], "high")
            self.assertEqual(last["mode"], "plan")
            self.assertEqual(last["delivery"], delivery)
            self.assertEqual(history["delivery"], delivery)
            self.assertEqual(history["effort"], "high")
            self.assertEqual(history["mode"], "plan")
        self.assertEqual(self.runner.calls, 2)

    async def test_removed_models_cannot_reappear_or_be_selected_or_run(self):
        self.assertTrue(all(not is_retired_model(model) for _, model, _ in OFFICIAL_MODELS))
        self.store.set_model_health({model: {"outcome": "success"} for model in REMOVED})
        _, keyboard = self.bridge._model_picker(12345)
        buttons = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertTrue(all(not is_retired_model(b.split(":", 1)[1]) for b in buttons))
        for model in (*REMOVED, "3.7", "3.6-low", "gpt", "120b"):
            await self.bridge.handle(update(f"/model {model}"))
            self.assertIsNone(self.store.get_model(12345))
            for prefix in ("model", "choose"):
                await self.bridge.handle(callback_update(f"{prefix}:{model}"))
                self.assertIsNone(self.store.get_model(12345))
        for model in REMOVED:
            self.store.set_model(12345, model)
            await self.bridge.handle(update("check the server"))
            self.assertIsNone(self.bridge.slot)
            self.assertEqual(self.store.get_model(12345), model)
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(resolve_model("sonnet"), "claude-sonnet-5-5-medium")
        self.assertEqual(resolve_model("opus"), "claude-opus-5-5-medium")

    async def test_refresh_never_probes_removed_models(self):
        self.runner.list_models = AsyncMock(return_value=(*REMOVED, "gemini-3.8-flash-high"))
        await self.bridge._refresh_model_health(12345)
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.runner.last_model, "gemini-3.8-flash-high")
        self.assertFalse(set(REMOVED) & self.store.get_model_health()["models"].keys())
