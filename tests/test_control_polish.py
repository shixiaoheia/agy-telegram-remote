"""Control-card lifecycle, repeated taps and explicit conversation reset."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    from .common import settings_at
    from .test_bot import FakeAPI, FakeRunner, callback_update, update
    from .test_telegram_formatting import validate
except ImportError:
    from common import settings_at
    from test_bot import FakeAPI, FakeRunner, callback_update, update
    from test_telegram_formatting import validate

from agy_runner import Result
from bot import Bridge, ControlCard, control_html
from state_store import Store
from telegram_api import TelegramError


class ControlPolishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed, self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        self.runner.finish.set()
        job = self.bridge.slot
        if job and job.worker:
            job.cancel.set()
            await job.worker
        if self.bridge.model_check_task:
            self.bridge._cancel_model_health()
            await asyncio.gather(self.bridge.model_check_task, return_exceptions=True)
        for pending in self.bridge.reset_confirmations.values():
            pending.timer.cancel()
        await self.drain()
        self.store.close()
        self.temp.cleanup()

    async def drain(self):
        if self.bridge.reply_worker:
            await self.bridge.reply_worker

    async def saved_result(self):
        record = self.store.save(12345, {"job_id": "a" * 12, "outcome": "success", "text": "saved **answer**"})
        self.store.save_history(12345, "saved task", record)
        return "history:" + record["job_id"]

    async def confirm_action(self):
        await self.bridge.handle(callback_update("tasks:new"))
        await self.drain()
        return self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]

    async def held_job(self):
        self.runner.hold = True
        await self.bridge.handle(update("original requirement"))
        await self.runner.started.wait()
        return self.bridge.slot

    async def test_result_double_tap_during_delivery_queues_one_copy(self):
        action = await self.saved_result()
        gate, entered = asyncio.Event(), asyncio.Event()
        original = self.bridge.send_html
        async def held(*args, **kwargs):
            entered.set()
            await gate.wait()
            return await original(*args, **kwargs)
        with patch.object(self.bridge, "send_html", side_effect=held):
            await self.bridge.handle(callback_update(action))
            await entered.wait()
            await self.bridge.handle(callback_update(action, cq_id="second"))
            gate.set()
            await self.drain()
        self.assertEqual(self.api.sent_count, 1)
        self.assertEqual(self.runner.calls, 0)
        self.assertIn("正在打开或刚刚已发送", self.api.answered_callbacks[-1][1])

    async def test_recent_result_tap_deduplicates_but_later_retrieval_is_allowed(self):
        action = await self.saved_result()
        await self.bridge.handle(callback_update(action))
        await self.drain()
        await self.bridge.handle(callback_update(action))
        await self.drain()
        self.assertEqual(self.api.sent_count, 1)
        later = time.monotonic() + 3
        with patch("bot.time.monotonic", return_value=later):
            await self.bridge.handle(callback_update(action))
            await self.drain()
        self.assertEqual(self.api.sent_count, 2)

    async def test_failed_result_delivery_releases_duplicate_guard(self):
        action = await self.saved_result()
        self.api.fail_at.add(1)
        await self.bridge.handle(callback_update(action))
        await self.drain()
        self.assertFalse(self.bridge._pending_results)
        await self.bridge.handle(callback_update(action))
        await self.drain()
        self.assertIn("answer", self.api.messages[-1][1])

    async def test_full_reply_queue_does_not_lock_result_retrieval(self):
        action = await self.saved_result()
        for _ in range(32):
            self.bridge.replies.put_nowait((12345, "queued"))
        await self.bridge.handle(callback_update(action))
        self.assertFalse(self.bridge._pending_results)
        await self.bridge._send_replies()
        await self.bridge.handle(callback_update(action))
        await self.drain()
        self.assertIn("answer", self.api.messages[-1][1])

    async def test_unchanged_status_edit_does_not_send_fallback(self):
        self.api.edit = AsyncMock(side_effect=TelegramError(400, not_modified=True))
        await self.bridge.handle(callback_update("status:summary"))
        await self.drain()
        self.assertEqual(self.api.sent_count, 0)

    async def test_repeated_selector_edit_failure_sends_one_replacement(self):
        self.api.edit = AsyncMock(side_effect=TelegramError(400))
        for _ in range(3):
            await self.bridge.handle(callback_update("status:summary"))
        await self.drain()
        self.assertEqual(self.api.sent_count, 1)

    def test_control_style_escapes_untrusted_text_and_preserves_code(self):
        rendered = control_html("📁 `<folder>`\n━━━━━━━━\n文件 `<b>&.txt`\n/steer <修正内容>")
        validate(rendered)
        self.assertIn("<code>&lt;folder&gt;</code>", rendered)
        self.assertIn("&lt;b&gt;&amp;.txt", rendered)
        self.assertNotIn("`", rendered)
        self.assertNotIn("━", rendered)

    async def test_usage_whitelist_and_details_use_valid_html(self):
        self.store.set_conversation(12345, "conversation-example")
        for text in ("/usage", "/whitelist", "/status details", "/help", "/ls"):
            await self.bridge.handle(update(text))
            await self.drain()
            validate(self.api.messages[-1][1])
            self.assertNotIn("`", self.api.messages[-1][1])
            self.assertEqual(self.api.parse_modes[-1], "HTML")
            if text == "/usage":
                self.assertIn("<blockquote>", self.api.messages[-1][1])

    async def test_card_finishing_during_send_updates_same_message(self):
        self.api.accept_gate = asyncio.Event()
        card = ControlCard(12345)
        self.bridge.queue_card(card, "🔎 正在检测", {"inline_keyboard": [[{"text": "取消", "callback_data": "old"}]]})
        await self.api.accept_entered.wait()
        self.bridge.queue_card(card, "✅ 检测完成")
        self.api.accept_gate.set()
        await self.drain()
        self.assertEqual(self.api.sent_count, 1)
        self.assertIn("检测完成", self.api.messages[0][1])
        self.assertEqual(self.api.messages[0][2]["inline_keyboard"], [])

    async def test_usage_card_does_not_interpret_text_from_saved_counters(self):
        with patch.object(self.store, "get_usage", return_value={"total_turns": "<b>literal</b>"}):
            await self.bridge.handle(update("/usage"))
            await self.drain()
        validate(self.api.messages[-1][1])
        self.assertIn("&lt;b&gt;literal&lt;/b&gt;", self.api.messages[-1][1])

    async def test_ambiguous_initial_card_send_is_never_replayed(self):
        self.api.fail_at.add(1)
        card = ControlCard(12345)
        self.bridge.queue_card(card, "正在检测")
        await self.drain()
        self.bridge.queue_card(card, "检测完成")
        await self.drain()
        self.assertEqual(self.api.sent_count, 1)

    async def test_probe_reuses_card_and_zero_success_is_warning(self):
        self.runner.hold = True
        self.runner.result = Result("error", category="network")
        self.store.set_model(12345, "gemini-3.8-flash-high")
        await self.bridge.handle(callback_update("probe:current"))
        await self.runner.started.wait()
        task = self.bridge.model_check_task
        self.runner.finish.set()
        await task
        await self.drain()
        self.assertEqual(self.api.sent_count, 1)
        final = self.api.messages[0]
        self.assertIn("⚠️", final[1])
        self.assertIn("0/1", final[1])
        actions = [b["callback_data"] for row in final[2]["inline_keyboard"] for b in row]
        self.assertEqual(actions, ["selector:models", "selector:close"])

    async def test_probe_cancel_finishes_same_card_without_old_buttons(self):
        self.runner.hold = True
        await self.bridge.handle(callback_update("probe:all"))
        await self.runner.started.wait()
        task, token = self.bridge.model_check_task, self.bridge.model_check_id
        await self.bridge.handle(callback_update("probe:cancel:" + token))
        await asyncio.gather(task, return_exceptions=True)
        await self.drain()
        self.assertEqual(self.api.sent_count, 1)
        self.assertIn("已取消", self.api.messages[0][1])
        self.assertNotIn("probe:cancel", str(self.api.messages[0][2]))

    async def test_new_supplement_reuses_card_and_only_latest_is_valid(self):
        job = await self.held_job()
        await self.bridge.handle(update("first supplement"))
        await self.drain()
        old = job.correction_token
        count = self.api.sent_count
        await self.bridge.handle(update("second supplement"))
        await self.drain()
        self.assertEqual(self.api.sent_count, count)
        self.assertIn("second supplement", self.api.messages[-1][1])
        await self.bridge.handle(callback_update(f"correction:apply:{job.job_id}:{old}"))
        self.assertFalse(job.cancel.is_set())

    async def test_supplement_expiry_removes_buttons_without_cancelling_job(self):
        job = await self.held_job()
        with patch("bot.CORRECTION_TTL_SECONDS", .02):
            await self.bridge.handle(update("pending supplement"))
            await asyncio.sleep(.05)
        await self.drain()
        self.assertFalse(job.cancel.is_set())
        self.assertEqual(job.pending_correction, "")
        self.assertIn("已过期", self.api.messages[-1][1])
        self.assertEqual(self.api.messages[-1][2]["inline_keyboard"], [])

    async def test_job_completion_retires_pending_supplement(self):
        job = await self.held_job()
        await self.bridge.handle(update("pending supplement"))
        await self.drain()
        card = job.correction_card
        self.runner.finish.set()
        await job.worker
        await self.drain()
        text = self.api.messages[self.api.message_indices[card.message_id]]
        self.assertIn("任务已结束", text[1])
        self.assertEqual(text[2]["inline_keyboard"], [])

    async def test_new_dialog_button_only_resets_after_confirmation(self):
        await self.saved_result()
        self.store.set_conversation(12345, "existing-conversation")
        action = await self.confirm_action()
        self.assertIsNotNone(self.store.get_conversation(12345))
        count = self.api.sent_count
        await self.bridge.handle(callback_update(action))
        await self.drain()
        self.assertIsNone(self.store.get_conversation(12345))
        self.assertIsNotNone(self.store.history_item(12345, "a" * 12))
        self.assertEqual(self.api.sent_count, count)
        self.store.set_conversation(12345, "newer-conversation")
        await self.bridge.handle(callback_update(action))
        self.assertEqual(self.store.get_conversation(12345)["conversation_id"], "newer-conversation")

    async def test_dismiss_reset_keeps_conversation_and_removes_buttons(self):
        self.store.set_conversation(12345, "existing-conversation")
        action = await self.confirm_action()
        await self.bridge.handle(callback_update(action.replace(":confirm:", ":dismiss:")))
        await self.drain()
        self.assertIsNotNone(self.store.get_conversation(12345))
        self.assertEqual(self.api.messages[-1][2]["inline_keyboard"], [])

    async def test_changed_conversation_invalidates_reset_confirmation(self):
        action = await self.confirm_action()
        self.store.set_conversation(12345, "changed-conversation")
        await self.bridge.handle(callback_update(action))
        await self.drain()
        self.assertIsNotNone(self.store.get_conversation(12345))
        self.assertIn("已发生变化", self.api.messages[-1][1])

    async def test_expired_and_foreign_reset_confirmation_cannot_reset(self):
        self.store.set_conversation(12345, "existing-conversation")
        action = await self.confirm_action()
        await self.bridge.handle(callback_update(action, user=67890, chat_id=67890))
        event = callback_update(action)
        event["callback_query"]["message"]["chat"]["type"] = "group"
        await self.bridge.handle(event)
        self.bridge.reset_confirmations[12345].expires = 0
        await self.bridge.handle(callback_update(action))
        self.assertIsNotNone(self.store.get_conversation(12345))
        self.assertEqual(self.runner.calls, 0)

    async def test_direct_new_command_remains_immediate(self):
        self.store.set_conversation(12345, "existing-conversation")
        await self.bridge.handle(update("/new"))
        self.assertIsNone(self.store.get_conversation(12345))
        self.assertFalse(self.bridge.reset_confirmations)
