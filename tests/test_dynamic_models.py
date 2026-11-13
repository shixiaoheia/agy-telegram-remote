import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot import Bridge
from state_store import Store
from tests.common import settings_at
from tests.test_bot import FakeAPI, FakeRunner, callback_update, update


class DynamicModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed,
                           self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.runner.list_models = AsyncMock(return_value=(
            "gemini-4-carbon-high", "gemini-4-carbon-low", "future-provider-xhigh"))
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

    async def handle(self, event):
        await self.bridge.handle(event)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker

    async def test_first_open_fetches_current_account_without_chat_probe(self):
        self.assertEqual(self.bridge._model_choices(), [])
        await self.handle(update("/model"))
        self.runner.list_models.assert_awaited_once()
        self.assertEqual(self.runner.calls, 0)
        ids = self.bridge._variant_ids()
        self.assertEqual(ids, frozenset(self.runner.list_models.return_value))
        self.assertNotIn("gemini-3.8-flash-high", ids)
        self.assertIn("自动刷新", self.api.messages[-1][1])

    async def test_cache_expiry_and_manual_refresh_replace_instead_of_merge(self):
        await self.handle(update("/model"))
        self.store.set_model(12345, "gemini-4-carbon-high")
        await self.handle(update("/model"))
        self.runner.list_models.assert_awaited_once()
        self.bridge.catalog_attempt_at -= 61
        self.runner.list_models.return_value = ("new-account-model-low",)
        await self.handle(update("/model"))
        self.assertEqual(self.runner.list_models.await_count, 2)
        self.assertEqual(self.bridge._variant_ids(), {"new-account-model-low"})
        await self.handle(update("/model refresh"))
        self.assertEqual(self.runner.list_models.await_count, 3)
        self.assertEqual(self.store.get_model(12345), "gemini-4-carbon-high")
        self.assertEqual(self.runner.calls, 0)

    async def test_failure_retains_snapshot_and_retries_with_backoff(self):
        await self.handle(update("/model"))
        snapshot = self.store.get_model_catalog()
        self.runner.list_models.side_effect = ValueError("invalid listing")
        await self.handle(update("/model refresh"))
        self.assertEqual(self.store.get_model_catalog(), snapshot)
        self.assertIn("缓存", self.api.messages[-1][1])
        await self.handle(update("/model"))
        self.assertEqual(self.runner.list_models.await_count, 2)
        self.bridge.catalog_attempt_at -= 16
        await self.handle(update("/model"))
        self.assertEqual(self.runner.list_models.await_count, 3)

    async def test_first_failure_never_invents_a_catalog_from_old_health(self):
        self.store.set_model_health({"old-model-high": {"outcome": "success"}})
        self.runner.list_models.side_effect = OSError("offline")
        await self.handle(update("/model"))
        self.assertEqual(self.bridge._model_choices(), [])
        self.assertIn("尚未获取模型列表", self.api.messages[-1][1])

    async def test_restart_uses_snapshot_but_fetches_again_on_first_open(self):
        await self.handle(update("/model"))
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)
        self.assertFalse(self.bridge.catalog_live)
        self.assertTrue(self.bridge._model_choices())
        await self.handle(update("/model"))
        self.assertEqual(self.runner.list_models.await_count, 2)

    async def test_busy_menu_defers_lookup_without_touching_running_job(self):
        from bot import Job
        job = Job(12345, 12345)
        self.bridge.slot = job
        await self.handle(update("/model refresh"))
        self.runner.list_models.assert_not_awaited()
        self.assertIs(self.bridge.slot, job)
        self.bridge.slot = None
        await self.handle(update("/model"))
        self.runner.list_models.assert_awaited_once()

    async def test_new_family_and_xhigh_buttons_are_selectable_without_extra_effort(self):
        await self.handle(update("/model"))
        await self.handle(callback_update("model:future-provider-xhigh"))
        buttons = self.api.messages[-1][2]["inline_keyboard"] if not self.api.edits else self.api.edits[-1][2]["inline_keyboard"]
        actions = [b["callback_data"] for row in buttons for b in row]
        self.assertIn("choose:future-provider-xhigh", actions)
        await self.handle(callback_update("choose:future-provider-xhigh"))
        self.assertEqual(self.store.get_model(12345), "future-provider-xhigh")
        self.assertIsNone(self.store.get_effort(12345))

    async def test_stale_button_cannot_navigate_or_select_a_removed_entry(self):
        await self.handle(update("/model"))
        self.runner.list_models.return_value = ("replacement-model-high",)
        await self.handle(update("/model refresh"))
        for prefix in ("model", "choose"):
            await self.handle(callback_update(f"{prefix}:gemini-4-carbon-high"))
        self.assertIsNone(self.store.get_model(12345))

    async def test_catalog_paginates_and_long_model_callbacks_fit_telegram_limit(self):
        long_model = "provider-" + "a" * 50 + "-high"
        self.runner.list_models.return_value = tuple(f"model-{i}-high" for i in range(25)) + (long_model,)
        await self.handle(update("/model"))
        for page in range(3):
            _, keyboard = self.bridge._model_picker(12345, page)
            self.assertLess(len(keyboard["inline_keyboard"]), 12)
            self.assertTrue(all(len(b["callback_data"].encode()) <= 64
                                for row in keyboard["inline_keyboard"] for b in row))
        action = self.bridge._model_callback(long_model, choosing=True)
        await self.handle(callback_update(action))
        self.assertEqual(self.store.get_model(12345), long_model)

    async def test_fresh_account_catalog_supersedes_old_entitlement_failure(self):
        self.store.set_model_health({"gemini-4-carbon-high": {"outcome": "error", "category": "entitlement", "checked_at": time.time() - 60}})
        await self.handle(update("/model"))
        self.assertFalse(self.bridge._model_is_known_unavailable("gemini-4-carbon-high"))

    async def test_catalog_validation_and_private_snapshot(self):
        models = ("future-model-high",)
        self.store.set_model_catalog(models)
        self.assertEqual(self.store.get_model_catalog()["models"], list(models))
        self.assertEqual((self.settings.state_dir / "model-catalog.json").stat().st_mode & 0o777, 0o600)
        for invalid in ([], ["bad;id"], ["same", "same"], ["model"] * 129):
            with self.assertRaises(ValueError):
                self.store.set_model_catalog(invalid)

    async def test_returned_slug_is_never_rewritten_by_a_legacy_alias(self):
        self.runner.list_models.return_value = ("claude-sonnet-5-5",)
        await self.handle(update("/model"))
        await self.handle(callback_update("model:claude-sonnet-5-5"))
        self.assertEqual(self.store.get_model(12345), "claude-sonnet-5-5")
        # No unsupported reasoning choices are invented for an unsuffixed entry.
        actions = [b["callback_data"] for row in self.api.edits[-1][2]["inline_keyboard"] for b in row]
        self.assertFalse(any(a.startswith(("choose:", "effort:")) for a in actions))
        await self.handle(update("/model sonnet"))
        self.assertEqual(self.store.get_model(12345), "claude-sonnet-5-5")
