import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .common import settings_at
except ImportError:
    from common import settings_at
from agy_runner import Result
from bot import Bridge
from state_store import Store
from telegram_api import TelegramError

def update(text, user=12345, chat_type="private", update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"type": chat_type, "id": user},
            "from": {"id": user, "is_bot": False},
            "text": text,
        },
    }

class FakeAPI:
    def __init__(self):
        self.messages = []
        self.sent_count = 0
        self.fail_at = set()
        self.backlog = []
        self.webhook = ""
        self.accept_gate = None

    async def send(self, chat_id, text):
        self.sent_count += 1
        if self.accept_gate and self.sent_count == 1:
            await self.accept_gate.wait()
        if self.sent_count in self.fail_at:
            raise TelegramError()
        self.messages.append((chat_id, text))

    async def call(self, method, **payload):
        if method == "getMe":
            return {"id": 100, "is_bot": True}
        if method == "getWebhookInfo":
            return {"url": self.webhook}
        if method == "getUpdates":
            return self.backlog
        raise AssertionError(method)

class FakeRunner:
    def __init__(self):
        self.calls = 0
        self.launched = 0
        self.blocked = False
        self.hold = False
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.result = Result("success", text="the result")

    async def run(self, prompt, cancel):
        self.calls += 1
        if cancel.is_set():
            return Result("cancelled", detail="cancelled before launch")
        self.launched += 1
        self.started.set()
        if self.hold:
            cancelled = asyncio.create_task(cancel.wait())
            finished = asyncio.create_task(self.finish.wait())
            await asyncio.wait([cancelled, finished], return_when=asyncio.FIRST_COMPLETED)
            for task in (cancelled, finished):
                task.cancel()
            await asyncio.gather(cancelled, finished, return_exceptions=True)
        return Result("cancelled") if cancel.is_set() else self.result

class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed,
                           self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        self.runner.finish.set()
        if self.bridge.slot:
            self.bridge.slot.cancel.set()
            worker = self.bridge.slot.worker
            if worker:
                await asyncio.wait_for(worker, 2)
        self.store.close()
        self.temp.cleanup()

    async def finish_job(self):
        if self.bridge.slot and self.bridge.slot.worker:
            await self.bridge.slot.worker

    async def test_bot_runtime_refuses_root(self):
        from bot import start
        from settings import ConfigError
        with patch("bot.os.geteuid", return_value=0), self.assertRaises(ConfigError):
            await start(self.settings, None)

    async def test_unauthorized_user_and_group_ignored(self):
        await self.bridge.handle(update("task", user=999))
        await self.bridge.handle(update("task", chat_type="group"))
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.api.messages, [])

    async def test_id_private_without_authorization(self):
        await self.bridge.handle(update("/id", user=999))
        self.assertIn("999", self.api.messages[0][1])
        self.assertEqual(self.runner.calls, 0)

    async def test_one_job_per_workspace(self):
        self.runner.hold = True
        await self.bridge.handle(update("task"))
        await self.runner.started.wait()
        await self.bridge.handle(update("other task", user=67890))
        self.assertEqual(self.runner.calls, 1)
        self.assertIn("已有任务", self.api.messages[-1][1])
        self.runner.finish.set()
        await self.finish_job()

    async def test_cancel_preparation_before_launch(self):
        self.api.accept_gate = asyncio.Event()
        task = asyncio.create_task(self.bridge.handle(update("task")))
        await asyncio.sleep(0)
        await self.bridge.handle(update("/cancel"))
        self.api.accept_gate.set()
        await task
        await self.finish_job()
        self.assertEqual(self.runner.launched, 0)

    async def test_cancel_active_task(self):
        self.runner.hold = True
        await self.bridge.handle(update("task"))
        await self.runner.started.wait()
        await self.bridge.handle(update("/cancel"))
        await self.finish_job()
        self.assertEqual(self.store.load(12345)["outcome"], "cancelled")
        self.assertEqual(self.runner.calls, 1)

    async def test_other_user_cannot_cancel(self):
        self.runner.hold = True
        await self.bridge.handle(update("task"))
        await self.runner.started.wait()
        await self.bridge.handle(update("/cancel", user=67890))
        self.assertFalse(self.bridge.slot.cancel.is_set())
        self.runner.finish.set()
        await self.finish_job()

    async def test_delivery_failure_keeps_result_without_rerun(self):
        self.api.fail_at = {2}
        await self.bridge.handle(update("task"))
        await self.finish_job()
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.store.load(12345)["delivery"], "failed_or_partial")
        await self.bridge.handle(update("/last"))
        self.assertEqual(self.runner.calls, 1)
        self.assertIn("the result", self.api.messages[-1][1])

    async def test_acceptance_failure_does_not_execute(self):
        self.api.fail_at = {1}
        await self.bridge.handle(update("task"))
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.store.load(12345)["outcome"], "not_started")

    async def test_result_saved_before_delivery(self):
        original_send = self.api.send
        async def checked(chat, text):
            if "the result" in text:
                self.assertEqual(self.store.load(12345)["text"], "the result")
            await original_send(chat, text)
        self.api.send = checked
        await self.bridge.handle(update("task"))
        await self.finish_job()

    async def test_last_cannot_read_other_user(self):
        self.store.save(12345, {"outcome": "success", "text": "secret"})
        await self.bridge.handle(update("/last", user=67890))
        self.assertNotIn("secret", self.api.messages[-1][1])
        self.assertEqual(self.runner.calls, 0)

    async def test_empty_result_does_not_say_task_completed_or_retry(self):
        self.runner.result = Result("no_text", detail="缺少回复")
        await self.bridge.handle(update("task"))
        await self.finish_job()
        self.assertEqual(self.runner.calls, 1)
        self.assertIn("没有自动重跑", self.api.messages[-1][1])
        self.assertNotIn("任务已完成", self.api.messages[-1][1])

    async def test_duplicate_update_not_dispatched_twice(self):
        await self.bridge.consume_updates([update("task", update_id=50)])
        await self.finish_job()
        await self.bridge.consume_updates([update("task", update_id=50)])
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.store.offset(), 51)

    async def test_watermark_write_failure_prevents_execution(self):
        with patch.object(self.store, "save_offset", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.bridge.consume_updates([update("task", update_id=50)])
        self.assertEqual(self.runner.calls, 0)

    async def test_startup_drops_backlog_and_recovers_interruption(self):
        self.api.backlog = [update("old task", update_id=200)]
        self.store.save(12345, {"outcome": "running"})
        await self.bridge.initialize()
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.bridge.offset, 201)
        self.assertEqual(self.store.load(12345)["outcome"], "interrupted")

    async def test_update_id_reset_after_idle(self):
        self.store.save_offset(100000)
        self.bridge.offset = 100000
        self.api.backlog = [update("old task", update_id=2)]
        await self.bridge.initialize()
        self.assertEqual(self.bridge.offset, 3)

    async def test_active_webhook_not_deleted(self):
        self.api.webhook = "https://example.invalid/webhook"
        with self.assertRaises(RuntimeError):
            await self.bridge.initialize()
        self.assertEqual(self.runner.calls, 0)

    async def test_storage_failure_blocks_new_tasks(self):
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            await self.bridge.handle(update("task"))
        self.assertTrue(self.runner.blocked)
        self.assertEqual(self.runner.calls, 0)

    async def test_invalid_or_oversized_task_not_launched(self):
        await self.bridge.handle(update("a" * (self.settings.max_prompt + 1)))
        await self.bridge.handle(update("null\x00byte"))
        self.assertEqual(self.runner.calls, 0)

    async def test_status_help_unknown_commands_do_not_run_agy(self):
        for text in ("/status", "/help", "/unknown"):
            await self.bridge.handle(update(text))
        self.assertEqual(self.runner.calls, 0)
