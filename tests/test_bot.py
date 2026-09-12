import asyncio
import os
import tempfile
import time
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
        self.last_model = None

    async def run(self, prompt, cancel, model=None):
        self.last_model = model
        self.calls += 1
        if cancel.is_set():
            return Result("cancelled", detail="cancelled before launch", model=model or "")
        self.launched += 1
        self.started.set()
        if self.hold:
            cancelled = asyncio.create_task(cancel.wait())
            finished = asyncio.create_task(self.finish.wait())
            await asyncio.wait([cancelled, finished], return_when=asyncio.FIRST_COMPLETED)
            for task in (cancelled, finished):
                task.cancel()
            await asyncio.gather(cancelled, finished, return_exceptions=True)
        res = Result("cancelled", model=model or "") if cancel.is_set() else self.result
        if model and not res.model:
            res.model = model
        return res

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
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

    async def handle(self, event):
        await self.bridge.handle(event)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker

    async def finish_job(self):
        if self.bridge.slot and self.bridge.slot.worker:
            await self.bridge.slot.worker

    async def test_bot_runtime_refuses_root(self):
        from bot import start
        from settings import ConfigError
        with patch("bot.os.geteuid", return_value=0), self.assertRaises(ConfigError):
            await start(self.settings, None)

    async def test_unauthorized_user_and_group_ignored(self):
        await self.handle(update("task", user=999))
        await self.handle(update("task", chat_type="group"))
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.api.messages, [])

    async def test_id_private_without_authorization(self):
        await self.handle(update("/id", user=999))
        self.assertIn("999", self.api.messages[0][1])
        self.assertEqual(self.runner.calls, 0)

    async def test_one_job_per_workspace(self):
        self.runner.hold = True
        await self.handle(update("task"))
        await self.runner.started.wait()
        await self.handle(update("other task", user=67890))
        self.assertEqual(self.runner.calls, 1)
        self.assertIn("已有任务", self.api.messages[-1][1])
        self.runner.finish.set()
        await self.finish_job()

    async def test_cancel_preparation_before_launch(self):
        self.api.accept_gate = asyncio.Event()
        task = asyncio.create_task(self.bridge.handle(update("task")))
        await asyncio.sleep(0)
        await self.handle(update("/cancel"))
        self.api.accept_gate.set()
        await task
        await self.finish_job()
        self.assertEqual(self.runner.launched, 0)

    async def test_cancel_active_task(self):
        self.runner.hold = True
        await self.handle(update("task"))
        await self.runner.started.wait()
        await self.handle(update("/cancel"))
        await self.finish_job()
        self.assertEqual(self.store.load(12345)["outcome"], "cancelled")
        self.assertEqual(self.runner.calls, 1)

    async def test_other_user_cannot_cancel(self):
        self.runner.hold = True
        await self.handle(update("task"))
        await self.runner.started.wait()
        await self.handle(update("/cancel", user=67890))
        self.assertFalse(self.bridge.slot.cancel.is_set())
        self.runner.finish.set()
        await self.finish_job()

    async def test_delivery_failure_keeps_result_without_rerun(self):
        self.api.fail_at = {2}
        await self.handle(update("task"))
        await self.finish_job()
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.store.load(12345)["delivery"], "failed_or_partial")
        await self.handle(update("/last"))
        self.assertEqual(self.runner.calls, 1)
        self.assertIn("the result", self.api.messages[-1][1])

    async def test_acceptance_failure_does_not_execute(self):
        self.api.fail_at = {1}
        await self.handle(update("task"))
        await self.finish_job()
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.store.load(12345)["outcome"], "not_started")

    async def test_result_saved_before_delivery(self):
        original_send = self.api.send
        async def checked(chat, text):
            if "the result" in text:
                self.assertEqual(self.store.load(12345)["text"], "the result")
            await original_send(chat, text)
        self.api.send = checked
        await self.handle(update("task"))
        await self.finish_job()

    async def test_last_cannot_read_other_user(self):
        self.store.save(12345, {"outcome": "success", "text": "secret"})
        await self.handle(update("/last", user=67890))
        self.assertNotIn("secret", self.api.messages[-1][1])
        self.assertEqual(self.runner.calls, 0)

    async def test_empty_result_does_not_say_task_completed_or_retry(self):
        self.runner.result = Result("no_text", detail="缺少回复")
        await self.handle(update("task"))
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
            await self.handle(update("task"))
        self.assertTrue(self.runner.blocked)
        self.assertEqual(self.runner.calls, 0)

    async def test_invalid_or_oversized_task_not_launched(self):
        await self.handle(update("a" * (self.settings.max_prompt + 1)))
        await self.handle(update("null\x00byte"))
        self.assertEqual(self.runner.calls, 0)

    async def test_status_help_unknown_commands_do_not_run_agy(self):
        for text in ("/status", "/help", "/unknown"):
            await self.handle(update(text))
        self.assertEqual(self.runner.calls, 0)

    async def test_update_id_smaller_than_offset_dropped_for_replay_protection(self):
        self.bridge.offset = 500
        await self.bridge.consume_updates([update("replayed task", update_id=400)])
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.bridge.offset, 500)


    async def test_slow_last_does_not_delay_cancel_from_polling(self):
        self.runner.hold = True
        await self.bridge.consume_updates([update("task", update_id=1)])
        await self.runner.started.wait()
        job = self.bridge.slot
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api.send

        async def slow_send(chat, text):
            if "任务执行中" in text:
                entered.set()
                await release.wait()
            await original(chat, text)

        self.api.send = slow_send
        try:
            await self.bridge.consume_updates([update("/last", update_id=2)])
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(
                self.bridge.consume_updates([update("/cancel", update_id=3)]), 1)
            self.assertTrue(job.cancel.is_set())
            self.assertEqual(self.store.offset(), 4)
            await self.finish_job()
            self.assertEqual(self.runner.calls, 1)
            self.assertEqual(self.store.load(12345)["outcome"], "cancelled")
        finally:
            release.set()
            if self.bridge.reply_worker:
                await self.bridge.reply_worker

    async def test_slow_acceptance_does_not_block_cancel_or_start_task(self):
        self.api.accept_gate = asyncio.Event()
        try:
            await self.bridge.consume_updates([update("task", update_id=1)])
            job = self.bridge.slot
            await asyncio.sleep(0)
            await asyncio.wait_for(
                self.bridge.consume_updates([update("/cancel", update_id=2)]), 1)
            self.assertTrue(job.cancel.is_set())
        finally:
            self.api.accept_gate.set()
        await self.finish_job()
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.store.load(12345)["outcome"], "not_started")

    async def test_reply_queue_is_bounded_and_cancel_survives_overload(self):
        self.runner.hold = True
        await self.bridge.consume_updates([update("task", update_id=1)])
        await self.runner.started.wait()
        job = self.bridge.slot
        for uid in range(2, 102):
            await self.bridge.consume_updates([update("/status", update_id=uid)])
        self.assertEqual(self.bridge.replies.qsize(), 32)
        await self.bridge.consume_updates([update("/cancel", update_id=102)])
        self.assertTrue(job.cancel.is_set())
        self.assertEqual(self.bridge.replies.qsize(), 32)
        await self.finish_job()

    async def test_public_id_has_global_cooldown_without_user_map(self):
        with patch("bot.time.monotonic", return_value=100):
            for user in range(1000, 1100):
                await self.bridge.consume_updates([update("/id", user=user, update_id=user)])
        await self.bridge.reply_worker
        self.assertEqual(len(self.api.messages), 1)
        with patch("bot.time.monotonic", return_value=103):
            await self.bridge.consume_updates([update("/id", user=2000, update_id=2000)])
        await self.bridge.reply_worker
        self.assertEqual(len(self.api.messages), 2)
        self.assertEqual(self.runner.calls, 0)

    async def test_model_command_show_current_and_examples(self):
        await self.handle(update("/model"))
        msg = self.api.messages[-1][1]
        self.assertIn("当前生效模型：默认（由 agy 决定）", msg)
        self.assertIn("gemini-3.8-flash-high", msg)
        self.assertIn("claude-opus-4-6-thinking", msg)
        self.assertIn("gpt-oss-120b-medium", msg)
        self.assertIn("官方支持的模型全列表", msg)

    async def test_model_command_switch_and_reset(self):
        await self.handle(update("/model claude-sonnet-4-6"))
        self.assertIn("已切换模型为：claude-sonnet-4-6", self.api.messages[-1][1])
        self.assertEqual(self.store.get_model(12345), "claude-sonnet-4-6")

        # Check /model reflects the new choice
        await self.handle(update("/model"))
        self.assertIn("当前生效模型：claude-sonnet-4-6", self.api.messages[-1][1])

        # Reset model
        await self.handle(update("/model default"))
        self.assertIn("已恢复为默认模型", self.api.messages[-1][1])
        self.assertIsNone(self.store.get_model(12345))

    async def test_model_command_alias_resolution(self):
        await self.handle(update("/model 3.8"))
        self.assertIn("已切换模型为：gemini-3.8-flash-high", self.api.messages[-1][1])
        self.assertEqual(self.store.get_model(12345), "gemini-3.8-flash-high")

        await self.handle(update("/model opus"))
        self.assertIn("已切换模型为：claude-opus-4-6-thinking", self.api.messages[-1][1])
        self.assertEqual(self.store.get_model(12345), "claude-opus-4-6-thinking")

        await self.handle(update("/model pro"))
        self.assertIn("已切换模型为：gemini-3.1-pro-high", self.api.messages[-1][1])
        self.assertEqual(self.store.get_model(12345), "gemini-3.1-pro-high")

    async def test_model_command_invalid_rejected(self):
        await self.handle(update("/model bad;char"))
        self.assertIn("模型名称格式不正确", self.api.messages[-1][1])
        self.assertIsNone(self.store.get_model(12345))

    async def test_job_uses_selected_model(self):
        await self.handle(update("/model gemini-3.1-pro-high"))
        await self.handle(update("write a script"))
        await self.finish_job()
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.runner.last_model, "gemini-3.1-pro-high")
        self.assertIn("gemini-3.1-pro-high", self.api.messages[-1][1])
        record = self.store.load(12345)
        self.assertEqual(record.get("model"), "gemini-3.1-pro-high")


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.ready_file = Path(self.temp.name) / "ready.json"
        self.api = FakeAPI()
        self.store = Store(self.settings.state_dir, self.settings.allowed,
                           self.settings.max_reply, 7, self.settings.token)
        self.runner = FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner, self.ready_file)

    async def asyncTearDown(self):
        self.bridge.stop.set()
        self.store.close()
        self.temp.cleanup()

    async def test_initialization_marks_not_polling_ready(self):
        await self.bridge.initialize()
        self.assertTrue(self.ready_file.is_file())
        from state_store import read_json
        data = read_json(self.ready_file, 1024)
        self.assertTrue(data.get("initialized"))
        self.assertFalse(data.get("polling_ready"))

        from manage import main
        with patch("sys.argv", ["manage.py", "check-ready", "--file", str(self.ready_file), "--pid", str(os.getpid())]):
            self.assertEqual(main(), 1)

    async def test_first_polling_success_marks_ready_and_exit_revokes(self):
        self.api.backlog = []
        original_call = self.api.call

        async def poll_call(method, **payload):
            if method == "getUpdates":
                await asyncio.sleep(0.01)
                return self.api.backlog
            return await original_call(method, **payload)

        self.api.call = poll_call
        task = asyncio.create_task(self.bridge.run())
        for _ in range(50):
            if self.bridge.polling_ready:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.bridge.polling_ready)

        from state_store import read_json
        data = read_json(self.ready_file, 1024)
        self.assertTrue(data.get("polling_ready"))

        from manage import main
        with patch("sys.argv", ["manage.py", "check-ready", "--file", str(self.ready_file), "--pid", str(os.getpid())]):
            self.assertEqual(main(), 0)

        # Stop the bridge and verify exit revokes ready file
        self.bridge.stop.set()
        await task
        self.assertFalse(self.ready_file.exists())

    async def test_first_polling_failure_does_not_mark_ready(self):
        async def failing_call(method, **payload):
            if method == "getMe":
                return {"id": 100, "is_bot": True}
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                raise TelegramError(code=401)
            raise AssertionError(method)
        self.api.call = failing_call

        with self.assertRaises(TelegramError):
            await self.bridge.run()

        self.assertFalse(self.bridge.polling_ready)
        self.assertFalse(self.ready_file.exists())

    async def test_first_round_messages_processed_exactly_once(self):
        self.api.backlog = [update("first message", update_id=10)]
        original_call = self.api.call

        async def poll_call(method, **payload):
            if method == "getUpdates":
                await asyncio.sleep(0.01)
                if payload.get("offset") == -1:
                    return []
                res = list(self.api.backlog)
                self.api.backlog = []
                return res
            return await original_call(method, **payload)

        self.api.call = poll_call
        task = asyncio.create_task(self.bridge.run())
        for _ in range(50):
            if self.bridge.polling_ready and self.runner.calls == 1:
                break
            await asyncio.sleep(0.02)

        self.assertTrue(self.bridge.polling_ready)
        self.assertEqual(self.runner.calls, 1)

        if self.bridge.slot and self.bridge.slot.worker:
            await self.bridge.slot.worker

        await asyncio.sleep(0.05)
        self.assertEqual(self.runner.calls, 1)
        self.bridge.stop.set()
        await task

    async def test_initialization_transient_failures_retry_then_recover(self):
        from unittest.mock import AsyncMock
        for code, retry in ((0, 0), (429, 7), (503, 0)):
            with self.subTest(code=code):
                initialize = AsyncMock(side_effect=[TelegramError(code, retry), None])
                async def elapsed(awaitable, timeout):
                    awaitable.close()
                    raise asyncio.TimeoutError()
                with patch.object(self.bridge, "initialize", initialize), patch(
                        "bot.asyncio.wait_for", side_effect=elapsed) as wait:
                    await self.bridge.initialize_with_retry()
                self.assertEqual(initialize.await_count, 2)
                self.assertEqual(wait.call_args.args[1] if len(wait.call_args.args) > 1
                                 else wait.call_args.kwargs["timeout"], max(1.0, retry))
                self.assertEqual(self.runner.calls, 0)

    async def test_initialization_permanent_errors_do_not_retry(self):
        from unittest.mock import AsyncMock
        for code in (400, 401, 403, 409):
            with self.subTest(code=code):
                initialize = AsyncMock(side_effect=TelegramError(code))
                with patch.object(self.bridge, "initialize", initialize):
                    with self.assertRaises(TelegramError):
                        await self.bridge.initialize_with_retry()
                self.assertEqual(initialize.await_count, 1)

    async def test_stop_interrupts_initialization_backoff(self):
        from unittest.mock import AsyncMock
        entered = asyncio.Event()
        async def fail():
            entered.set()
            raise TelegramError(429, 86400)
        with patch.object(self.bridge, "initialize", side_effect=fail):
            task = asyncio.create_task(self.bridge.run())
            await entered.wait()
            self.bridge.stop.set()
            await asyncio.wait_for(task, 1)
        self.assertFalse(self.bridge.polling_ready)
        self.assertFalse(self.ready_file.exists())

    async def test_shutdown_cancels_pending_control_delivery(self):
        entered = asyncio.Event()
        async def blocked_send(*args):
            entered.set()
            await asyncio.Event().wait()
        self.api.send = blocked_send
        self.bridge.queue_reply(12345, "pending")
        await entered.wait()
        self.bridge.stop.set()
        await self.bridge.run()
        self.assertTrue(self.bridge.reply_worker.done())
