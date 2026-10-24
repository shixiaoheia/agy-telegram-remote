"""Interaction regressions: concise cards and explicitly confirmed actions."""
import asyncio
import math
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    from .common import settings_at
    from .test_bot import FakeAPI, FakeRunner, callback_update, update
except ImportError:
    from common import settings_at
    from test_bot import FakeAPI, FakeRunner, callback_update, update
from bot import Bridge, Job, history_summary, record_time_label
from settings import OFFICIAL_MODELS
from state_store import Store, atomic_json


class InteractionUXTests(unittest.IsolatedAsyncioTestCase):
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
        self.runner.finish.set()
        if self.bridge.slot:
            self.bridge.slot.cancel.set()
            if self.bridge.slot.worker:
                await asyncio.wait_for(self.bridge.slot.worker, 2)
        if self.bridge.steer_tasks:
            await asyncio.gather(*tuple(self.bridge.steer_tasks), return_exceptions=True)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

    async def _drain(self):
        if self.bridge.reply_worker:
            await self.bridge.reply_worker

    async def _held_job(self):
        self.runner.hold = True
        await self.bridge.handle(update("original task"))
        job = self.bridge.slot
        await self.runner.started.wait()
        return job

    async def _offer(self, text="private additional requirement"):
        await self.bridge.handle(update(text))
        await self._drain()
        return self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]

    async def test_status_summary_keeps_task_focus_and_details_are_available(self):
        job = await self._held_job()
        await self.bridge.handle(update("/status"))
        await self._drain()
        summary = self.api.messages[-1][1]
        self.assertIn("任务：original task", summary)
        self.assertIn("阶段：", summary)
        self.assertIn("已运行：", summary)
        self.assertIn("模型：", summary)
        self.assertNotIn("额外可写目录（配置值）", summary)
        self.assertLess(len(summary), 600)
        await self.bridge.handle(callback_update("status:details"))
        details = self.api.edits[-1][1]
        self.assertIn("额外可写目录（配置值）", details)
        self.assertIn("服务启动：", details)
        await self.bridge.handle(callback_update("status:summary"))
        self.assertIn("任务：original task", self.api.edits[-1][1])
        self.assertIs(self.bridge.slot, job)
        self.assertEqual(self.runner.calls, 1)

    async def test_status_summary_describes_receiving_sending_cancel_and_handoff(self):
        job = Job(12345, 12345, history_title="task")
        self.bridge.slot = job
        for phase, expected in (("receiving", "接收附件"), ("saving", "保存结果"),
                                ("sending", "结果已完成，正在发送")):
            job.phase = phase
            self.assertIn(expected, self.bridge._status_card(12345)[0])
        job.cancel.set()
        self.assertIn("正在取消并清理", self.bridge._status_card(12345)[0])
        job.steered = True
        job.steer_task = asyncio.create_task(asyncio.Event().wait())
        try:
            self.assertIn("正在清理并交接", self.bridge._status_card(12345)[0])
        finally:
            job.steer_task.cancel()
            await asyncio.gather(job.steer_task, return_exceptions=True)

    async def test_status_readonly_buttons_do_not_expose_other_users_task(self):
        self.bridge.slot = Job(12345, 12345, history_title="PRIVATE_TITLE")
        await self.bridge.handle(callback_update("status:summary", user=67890, chat_id=67890))
        self.assertNotIn("PRIVATE_TITLE", self.api.edits[-1][1])
        self.assertIn("其他白名单用户", self.api.edits[-1][1])
        self.assertEqual(self.runner.calls, 0)

    async def test_status_edit_failure_sends_usable_details_card(self):
        from telegram_api import TelegramError
        self.api.edit = AsyncMock(side_effect=TelegramError())
        await self.bridge.handle(callback_update("status:details"))
        await self._drain()
        self.assertIn("工具自动审批", self.api.messages[-1][1])
        self.assertTrue(self.api.messages[-1][2]["inline_keyboard"])

    async def test_busy_supplement_only_runs_after_confirmation_and_only_once(self):
        old = await self._held_job()
        action = await self._offer()
        self.assertEqual(self.runner.calls, 1)
        self.assertFalse(old.cancel.is_set())
        self.assertNotIn("private additional requirement", action)
        self.assertLessEqual(len(action.encode()), 64)
        self.assertFalse(any("private additional requirement" in p.read_text()
                             for p in self.settings.state_dir.glob("*.json")))
        await self.bridge.handle(callback_update(action))
        await self.bridge.handle(callback_update(action, cq_id="double-tap"))
        await old.steer_task
        replacement = self.bridge.slot
        self.runner.finish.set()
        await replacement.worker
        self.assertEqual(self.runner.calls, 2)
        self.assertIn("original task", self.runner.last_prompt)
        self.assertIn("private additional requirement", self.runner.last_prompt)
        self.assertEqual(old.pending_correction, "")

    async def test_latest_supplement_invalidates_old_button_and_can_be_dismissed(self):
        job = await self._held_job()
        first = await self._offer("first addition")
        second = await self._offer("second addition")
        await self.bridge.handle(callback_update(first))
        self.assertFalse(job.cancel.is_set())
        self.assertEqual(job.pending_correction, "second addition")
        await self.bridge.handle(callback_update(second.replace(":apply:", ":dismiss:")))
        self.assertEqual(job.pending_correction, "")
        self.assertFalse(job.cancel.is_set())
        self.assertEqual(self.runner.calls, 1)

    async def test_expired_supplement_cannot_cancel_or_replace_job(self):
        job = await self._held_job()
        action = await self._offer()
        job.correction_expires = time.monotonic() - 1
        await self.bridge.handle(callback_update(action))
        self.assertFalse(job.cancel.is_set())
        self.assertIsNone(job.steer_task)
        self.assertEqual(self.runner.calls, 1)

    async def test_supplement_buttons_are_user_private_chat_and_job_bound(self):
        job = await self._held_job()
        action = await self._offer()
        events = [callback_update(action, user=67890, chat_id=67890),
                  callback_update(action, user=99999, chat_id=99999), callback_update(action)]
        events[-1]["callback_query"]["message"]["chat"]["type"] = "group"
        for event in events:
            await self.bridge.handle(event)
            self.assertFalse(job.cancel.is_set())
        self.runner.finish.set()
        await job.worker
        self.runner.finish.clear()
        await self.bridge.handle(update("different job"))
        replacement = self.bridge.slot
        await self.bridge.handle(callback_update(action))
        self.assertFalse(replacement.cancel.is_set())
        self.assertIsNone(replacement.steer_task)

    async def test_sending_result_and_attachment_tasks_do_not_offer_correction(self):
        job = await self._held_job()
        job.phase = "sending"
        await self.bridge.handle(update("addition after execution"))
        self.assertEqual(job.correction_token, "")
        await self.bridge.handle(update("/steer cannot replace completed result"))
        self.assertFalse(job.cancel.is_set())
        job.phase = "running"
        job.has_attachment = True
        await self.bridge.handle(update("addition to attachment task"))
        self.assertEqual(job.correction_token, "")

    async def test_other_user_busy_text_does_not_create_supplement(self):
        job = await self._held_job()
        await self.bridge.handle(update("other users addition", user=67890))
        self.assertEqual(job.correction_token, "")
        self.assertEqual(job.pending_correction, "")
        self.assertFalse(job.cancel.is_set())

    async def test_oversized_combined_correction_does_not_cancel_original(self):
        job = await self._held_job()
        await self.bridge.handle(update("Z" * self.settings.max_prompt))
        self.assertFalse(job.cancel.is_set())
        self.assertEqual(job.correction_token, "")
        await self.bridge.handle(update("/steer " + "Z" * self.settings.max_prompt))
        self.assertFalse(job.cancel.is_set())
        self.assertEqual(self.runner.calls, 1)

    async def test_history_distinguishes_same_titles_by_time_model_and_delivery(self):
        now = datetime(2026, 10, 23, 22, 0).timestamp()
        records = [{"title": "same task", "job_id": "a" * 12, "outcome": "success",
                    "updated_at": now - 1800, "model": "claude-opus-5-5-medium", "delivery": "failed_or_partial"},
                   {"title": "same task", "job_id": "b" * 12, "outcome": "interrupted",
                    "updated_at": now - 86400, "model": "gemini-3.8-flash-high", "delivery": "unknown"}]
        with patch("bot.time.time", return_value=now):
            text, keyboard = history_summary(records)
        for expected in ("今天21:30", "昨天22:00", "Opus 5.5", "Flash 3.8", "未完整送达", "投递未确认"):
            self.assertIn(expected, text)
        self.assertEqual([row[0]["callback_data"] for row in keyboard["inline_keyboard"]],
                         ["history:" + "a" * 12, "history:" + "b" * 12])

    async def test_history_invalid_time_is_labelled_unknown_without_failure(self):
        for stamp in (None, math.nan, math.inf, -1, 0, True, 10 ** 400):
            self.assertEqual(record_time_label(stamp), "时间未知")
        text, _ = history_summary([{"outcome": "success", "updated_at": math.inf, "delivery": {}}])
        self.assertIn("时间未知", text)

    async def test_current_model_probe_merges_only_selected_result_and_preserves_preferences(self):
        self.store.set_model(12345, "gemini-3.8-flash-high")
        self.store.set_mode(12345, "plan")
        old_time = time.time() - 7200
        self.store.set_model_health({"claude-opus-5-5-medium": {
            "outcome": "success", "category": "", "checked_at": old_time}})
        self.runner.list_models = AsyncMock()
        await self.bridge.handle(callback_update("probe:current"))
        task = self.bridge.model_check_task
        self.assertIsNotNone(task)
        await task
        await self._drain()
        self.assertEqual(self.runner.calls, 1)
        self.runner.list_models.assert_not_awaited()
        health = self.store.get_model_health()["models"]
        self.assertEqual(len(health), 2)
        self.assertEqual(health["claude-opus-5-5-medium"]["checked_at"], old_time)
        self.assertGreater(health["gemini-3.8-flash-high"]["checked_at"], old_time)
        self.assertEqual(self.store.get_model(12345), "gemini-3.8-flash-high")
        self.assertEqual(self.store.get_mode(12345), "plan")
        self.assertIn("1/1", self.api.messages[-1][1])

    async def test_all_models_button_uses_existing_full_detection_path(self):
        await self.bridge.handle(callback_update("probe:all"))
        await self.bridge.model_check_task
        await self._drain()
        self.assertEqual(self.runner.calls, len(OFFICIAL_MODELS))
        self.assertIsNone(self.store.get_model(12345))
        self.assertTrue(any("会消耗模型额度" in message[1] for message in self.api.sent_messages))

    async def test_model_card_shows_per_model_age_and_detection_actions(self):
        now = time.time()
        self.store.set_model(12345, "gemini-3.8-flash-high")
        text, _ = self.bridge._model_picker(12345)
        self.assertIn("未检测", text)
        self.store.set_model_health({"gemini-3.8-flash-high": {
            "outcome": "success", "category": "", "checked_at": now - 7200},
            "claude-opus-5-5-medium": {"outcome": "success", "category": "", "checked_at": now}})
        with patch("bot.time.time", return_value=now):
            text, keyboard = self.bridge._model_picker(12345)
        self.assertIn("2 小时前", text)
        actions = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertIn("probe:current", actions)
        self.assertIn("probe:all", actions)
        self.assertIn("消耗模型额度", text)
        self.assertEqual(self.runner.calls, 0)

    async def test_legacy_probe_cache_gets_its_existing_timestamp(self):
        atomic_json(self.settings.state_dir / "model-health.json", {"checked_at": 1234567890,
                    "models": {"gemini-3.8-flash-high": {"outcome": "success", "category": ""}}})
        self.assertEqual(self.store.get_model_health()["models"]["gemini-3.8-flash-high"]["checked_at"],
                         1234567890)

    async def test_model_probe_buttons_reject_default_retired_and_blocked_states(self):
        await self.bridge.handle(callback_update("probe:current"))
        self.assertIsNone(self.bridge.model_check_task)
        self.store.set_model(12345, "claude-sonnet-4-6")
        await self.bridge.handle(callback_update("probe:current"))
        self.assertIsNone(self.bridge.model_check_task)
        self.store.set_model(12345, "gemini-3.8-flash-high")
        self.runner.blocked = True
        await self.bridge.handle(callback_update("probe:current"))
        self.assertIsNone(self.bridge.model_check_task)
        self.assertEqual(self.runner.calls, 0)

    async def test_detection_cancel_button_is_request_and_user_bound(self):
        self.store.set_model(12345, "gemini-3.8-flash-high")
        self.runner.hold = True
        await self.bridge.handle(callback_update("probe:current"))
        first = self.bridge.model_check_task
        old_action = "probe:cancel:" + self.bridge.model_check_id
        await self.runner.started.wait()
        await self.bridge.handle(callback_update(old_action, user=67890, chat_id=67890))
        self.assertFalse(self.bridge.model_check_cancelling)
        await self.bridge.handle(callback_update(old_action))
        await asyncio.gather(first, return_exceptions=True)
        await self.bridge.handle(callback_update("probe:current"))
        second = self.bridge.model_check_task
        await self.bridge.handle(callback_update(old_action))
        self.assertFalse(self.bridge.model_check_cancelling)
        self.assertFalse(second.done())
        await self.bridge.handle(callback_update("probe:cancel:" + self.bridge.model_check_id))
        await asyncio.gather(second, return_exceptions=True)

    async def test_detection_actions_keep_private_chat_authorization(self):
        event = callback_update("probe:all")
        event["callback_query"]["message"]["chat"]["type"] = "group"
        await self.bridge.handle(event)
        await self.bridge.handle(callback_update("probe:all", user=99999, chat_id=99999))
        self.assertIsNone(self.bridge.model_check_task)
        self.assertEqual(self.runner.calls, 0)
