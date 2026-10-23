"""Regressions for control responsiveness, progress recovery and stored results."""
import asyncio
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
from bot import Bridge, Job
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
        self.runner.finish.set()
        if self.bridge.slot:
            self.bridge.slot.cancel.set()
            if self.bridge.slot.worker:
                await asyncio.wait_for(self.bridge.slot.worker, 2)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

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

        async def recovered(*args):
            job.cancel.set()

        self.api.edit = AsyncMock(side_effect=[TelegramError(), TelegramError(503), None])
        # End only after the recovered edit, without introducing real timer waits.
        calls = 0

        async def edit(*args):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TelegramError(0 if calls == 1 else 503)
            await recovered()

        self.api.edit = AsyncMock(side_effect=edit)
        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.bridge._refresh_progress(job)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [5, 10, 20])
        self.assertEqual(self.api.edit.await_count, 3)
        self.assertEqual(self.runner.calls, 0)

    async def test_progress_respects_429_and_stops_for_deleted_card(self):
        job = Job(12345, 12345, status_message_id=1)
        self.bridge.slot = job
        self.api.edit = AsyncMock(side_effect=[TelegramError(429, 17), TelegramError(400)])
        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.bridge._refresh_progress(job)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [5, 17])
        self.assertEqual(self.api.edit.await_count, 2)

    async def test_last_and_history_keep_settings_and_final_delivery(self):
        self.store.set_model(12345, "custom-model")
        self.store.set_effort(12345, "high")
        self.store.set_mode(12345, "plan")
        for delivery in ("sent", "failed_or_partial"):
            if delivery != "sent":
                self.api.fail_at.add(self.api.sent_count + 2)
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
