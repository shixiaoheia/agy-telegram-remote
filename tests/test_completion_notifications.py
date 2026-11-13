"""Task completion must create a new, normally notifying Telegram message."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

try:
    from .common import settings_at
    from .test_bot import FakeRunner, update
except ImportError:
    from common import settings_at
    from test_bot import FakeRunner, update

from agy_runner import Result
from bot import Bridge
from state_store import Store
from telegram_api import TelegramAPI, TelegramError


class CompletionNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed,
                           self.settings.max_reply, 7, self.settings.token)
        self.api = TelegramAPI(self.settings.token)
        self.requests = []
        self.message_id = 0
        self.saved_before_send = []
        self.drop_result = False
        self.api.call = AsyncMock(side_effect=self.request)
        self.runner = FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)
        await self.bridge._ensure_model_catalog()

    async def request(self, method, **payload):
        # Exercise the real send/edit payload builders, without a real token or network.
        self.requests.append((method, payload))
        if method == "sendMessage":
            self.message_id += 1
            if "NOTIFICATION_RESULT" in payload["text"]:
                self.saved_before_send.append(self.store.load(12345))
                if self.drop_result:
                    raise TelegramError()
            return {"message_id": self.message_id}
        if method in {"editMessageText", "setMyCommands", "sendChatAction"}:
            return True
        raise AssertionError(method)

    async def asyncTearDown(self):
        self.runner.finish.set()
        if self.bridge.slot and self.bridge.slot.worker:
            self.bridge.slot.cancel.set()
            await asyncio.wait_for(self.bridge.slot.worker, 2)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.api.close()
        self.store.close()
        self.temp.cleanup()

    async def complete(self):
        await self.bridge.handle(update("notification test"))
        job = self.bridge.slot
        await job.worker
        return job

    async def test_success_and_unsuccessful_outcomes_send_new_result_with_notifications(self):
        for outcome in ("success", "error", "timed_out", "cancelled"):
            with self.subTest(outcome=outcome):
                self.requests.clear()
                self.runner.result = Result(outcome, text="NOTIFICATION_RESULT")
                job = await self.complete()
                sends = [payload for method, payload in self.requests if method == "sendMessage"]
                self.assertEqual(len(sends), 2)  # acceptance, then the final result
                self.assertIn("NOTIFICATION_RESULT", sends[-1]["text"])
                self.assertFalse(sends[-1].get("disable_notification", False))
                self.assertEqual(sends[-1]["chat_id"], 12345)
                self.assertEqual(sends[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                                 f"history:{job.job_id}")
                edits = [payload for method, payload in self.requests if method == "editMessageText"]
                self.assertEqual(edits[-1]["message_id"], job.status_message_id)
                self.assertNotIn("NOTIFICATION_RESULT", edits[-1]["text"])
                self.assertNotIn("cancel:", str(edits[-1]["reply_markup"]))
                self.assertEqual(self.store.history_item(12345, job.job_id)["delivery"], "sent")
        self.assertEqual(self.runner.calls, 4)

    async def test_result_is_saved_before_notification_and_not_duplicated_in_progress(self):
        self.runner.result = Result("success", text="NOTIFICATION_RESULT " + self.settings.token)
        await self.complete()
        self.assertEqual(len(self.saved_before_send), 1)
        self.assertEqual(self.saved_before_send[0]["delivery"], "pending")
        self.assertIn("NOTIFICATION_RESULT", self.saved_before_send[0]["text"])
        self.assertNotIn(self.settings.token, str(self.requests))
        self.assertEqual(sum("NOTIFICATION_RESULT" in p.get("text", "") for _, p in self.requests), 1)

    async def test_progress_edit_failure_does_not_suppress_or_duplicate_final_message(self):
        self.api.edit = AsyncMock(side_effect=TelegramError())
        self.runner.result = Result("success", text="NOTIFICATION_RESULT")
        await self.complete()
        final = [p for m, p in self.requests if m == "sendMessage" and "NOTIFICATION_RESULT" in p["text"]]
        self.assertEqual(len(final), 1)
        self.assertFalse(final[0].get("disable_notification", False))
        self.assertEqual(self.store.load(12345)["delivery"], "sent")
        self.assertEqual(self.runner.calls, 1)

    async def test_ambiguous_notification_failure_is_not_replayed_and_last_only_retrieves(self):
        self.drop_result = True
        self.runner.result = Result("success", text="NOTIFICATION_RESULT")
        job = await self.complete()
        final = [p for m, p in self.requests if m == "sendMessage" and "NOTIFICATION_RESULT" in p["text"]]
        self.assertEqual(len(final), 1)
        self.assertEqual(self.store.history_item(12345, job.job_id)["delivery"], "failed_or_partial")
        self.assertIn("结果发送失败或不完整", self.requests[-1][1]["text"])
        self.drop_result = False
        await self.bridge.handle(update("/last"))
        await self.bridge.reply_worker
        self.assertEqual(self.runner.calls, 1)
        self.assertIn("NOTIFICATION_RESULT", self.requests[-1][1]["text"])

    async def test_superseded_task_does_not_send_a_completion_notification(self):
        self.runner.hold = True
        await self.bridge.handle(update("task to correct"))
        await self.runner.started.wait()
        job = self.bridge.slot
        job.steered = True
        job.cancel.set()
        await job.worker
        self.assertEqual(sum(m == "sendMessage" for m, _ in self.requests), 1)
        self.assertEqual(self.store.load(12345)["delivery"], "superseded_by_steer")

    async def test_english_result_is_a_new_message_with_localized_buttons(self):
        self.store.set_language(12345, "en")
        self.runner.result = Result("error", text="NOTIFICATION_RESULT")
        await self.complete()
        sends = [p for m, p in self.requests if m == "sendMessage"]
        self.assertEqual(len(sends), 2)
        self.assertIn("Task failed", sends[-1]["text"])
        self.assertIn("View result", str(sends[-1]["reply_markup"]))
        self.assertFalse(sends[-1].get("disable_notification", False))
