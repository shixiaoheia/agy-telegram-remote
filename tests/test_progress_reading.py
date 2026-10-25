"""Keep a running task's expanded process readable without automatic redraws."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .common import settings_at
    from .test_bot import FakeAPI, FakeRunner, callback_update, update
    from .test_telegram_formatting import validate
except ImportError:
    from common import settings_at
    from test_bot import FakeAPI, FakeRunner, callback_update, update
    from test_telegram_formatting import validate

from bot import Bridge, Job
from state_store import Store
from telegram_api import TelegramError


class ProgressReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed, self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)
        self.interval = patch("bot.PROGRESS_UPDATE_SECONDS", .01)
        self.interval.start()

    async def asyncTearDown(self):
        self.runner.finish.set()
        job = self.bridge.slot
        if job and job.worker:
            job.cancel.set()
            await asyncio.wait_for(job.worker, 3)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.interval.stop()
        self.store.close()
        self.temp.cleanup()

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 2)

    async def held_job(self):
        self.runner.hold = True
        self.runner.started.clear()
        self.runner.finish.clear()
        await self.bridge.handle(update("inspect this task"))
        await asyncio.wait_for(self.runner.started.wait(), 2)
        return self.bridge.slot

    def card(self, job):
        return self.api.messages[self.api.message_indices[job.status_message_id]]

    async def action(self, job, action):
        await self.bridge.handle(callback_update(f"progress:{action}:{job.job_id}", message_id=job.status_message_id))

    async def expanded(self, job):
        await self.action(job, "expand")
        await self.until(lambda: "📖 阅读中" in self.card(job)[1])

    def test_compact_and_expanded_trace_never_use_native_folding(self):
        job = Job(12345, 12345, activity="<tool> " + self.settings.token,
                  execution_steps=[f"step {i} " + self.settings.token for i in range(10)])
        compact = self.bridge._progress_text(job)
        self.assertIn("step 9", compact)
        self.assertNotIn("step 8", compact)
        job.progress_expanded = True
        expanded = self.bridge._progress_text(job)
        self.assertIn("step 4", expanded)
        self.assertNotIn("step 3", expanded)
        for text in (compact, expanded):
            self.assertNotIn("expandable", text)
            self.assertNotIn(self.settings.token, text)
            self.assertIn("&lt;tool&gt;", text)
            self.assertLess(len(validate(text).encode("utf-16-le")) // 2, 3400)

    async def test_expand_holds_visible_content_while_task_keeps_running(self):
        job = await self.held_job()
        job.execution_steps = ["✅ original step"]
        await self.expanded(job)
        shown, edits = self.card(job)[1], len(self.api.edits)
        job.execution_steps.append("✅ new background step")
        job.activity = "new background activity"
        await asyncio.sleep(.06)
        self.assertEqual(self.card(job)[1], shown)
        self.assertEqual(len(self.api.edits), edits)
        self.assertFalse(job.cancel.is_set())
        self.assertFalse(job.worker.done())
        self.assertEqual(self.runner.calls, 1)

    async def test_manual_refresh_preserves_expansion_and_uses_same_message(self):
        job = await self.held_job()
        await self.expanded(job)
        sent = self.api.sent_count
        job.execution_steps.append("✅ newest step")
        await self.action(job, "refresh")
        await self.until(lambda: "newest step" in self.card(job)[1])
        self.assertTrue(job.progress_expanded)
        self.assertNotIn("expandable", self.card(job)[1])
        self.assertIn("progress:collapse:", str(self.card(job)[2]))
        self.assertEqual(self.api.sent_count, sent)

    async def test_collapse_resumes_automatic_updates_and_compact_trace(self):
        job = await self.held_job()
        job.execution_steps = ["✅ older step", "✅ latest step"]
        await self.expanded(job)
        await self.action(job, "collapse")
        await self.until(lambda: "progress:expand:" in str(self.card(job)[2]))
        self.assertNotIn("older step", self.card(job)[1])
        job.activity = "automatic update resumed"
        await self.until(lambda: "automatic update resumed" in self.card(job)[1])
        self.assertFalse(job.progress_expanded)

    async def test_repeated_expand_is_idempotent_and_cancel_stays_in_fixed_row(self):
        job = await self.held_job()
        await self.expanded(job)
        for _ in range(3):
            await self.action(job, "expand")
        await self.until(lambda: not job.progress_requested.is_set())
        self.assertTrue(job.progress_expanded)
        for markup in (self.api.sent_messages[0][2], self.card(job)[2]):
            self.assertEqual([len(row) for row in markup["inline_keyboard"]], [2, 1])
            self.assertEqual(markup["inline_keyboard"][-1][0]["callback_data"], f"cancel:{job.job_id}")
        self.assertEqual(self.api.sent_count, 1)

    async def test_view_buttons_are_private_user_job_and_message_bound(self):
        job = await self.held_job()
        await self.expanded(job)
        data = f"progress:collapse:{job.job_id}"
        events = [callback_update(data, user=67890, chat_id=67890, message_id=job.status_message_id),
                  callback_update(data, message_id=job.status_message_id + 100),
                  callback_update("progress:collapse:" + "0" * 12, message_id=job.status_message_id),
                  callback_update(data, message_id=job.status_message_id)]
        events[-1]["callback_query"]["message"]["chat"]["type"] = "group"
        for event in events:
            await self.bridge.handle(event)
            self.assertTrue(job.progress_expanded)
        self.assertEqual(self.runner.calls, 1)

    async def test_old_task_buttons_cannot_redraw_result_or_change_next_task(self):
        first = await self.held_job()
        self.runner.finish.set()
        await first.worker
        final = self.card(first)
        await self.action(first, "expand")
        self.assertEqual(self.card(first), final)
        second = await self.held_job()
        await self.action(first, "expand")
        self.assertFalse(second.progress_expanded)
        self.assertEqual(self.runner.calls, 2)

    async def test_expand_waits_for_old_automatic_edit_without_blocking_callback(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api.edit
        delayed = False
        async def edit(*args, **kwargs):
            nonlocal delayed
            if not delayed:
                delayed = True
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)
        self.api.edit = edit
        job = await self.held_job()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(self.action(job, "expand"), .5)
            self.assertTrue(job.progress_expanded)
        finally:
            release.set()
        await self.until(lambda: "📖 阅读中" in self.card(job)[1])
        shown = self.card(job)[1]
        await asyncio.sleep(.05)
        self.assertEqual(self.card(job)[1], shown)

    async def test_completion_drains_requested_edit_before_replacing_card(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api.edit
        async def edit(*args, **kwargs):
            if "📖 阅读中" in args[2]:
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)
        self.api.edit = edit
        job = await self.held_job()
        try:
            await self.action(job, "expand")
            await asyncio.wait_for(entered.wait(), 2)
            self.runner.finish.set()
            await asyncio.sleep(.02)
            self.assertFalse(job.worker.done())
        finally:
            release.set()
        await asyncio.wait_for(job.worker, 2)
        self.assertIn("the result", self.card(job)[1])
        self.assertNotIn("📖 阅读中", self.card(job)[1])
        self.assertNotIn("progress:", str(self.card(job)[2]))

    async def test_cancel_is_responsive_while_reading_and_never_reexecutes(self):
        job = await self.held_job()
        await self.expanded(job)
        await self.bridge.handle(callback_update(f"cancel:{job.job_id}", message_id=job.status_message_id))
        self.assertTrue(job.cancel.is_set())
        await asyncio.wait_for(job.worker, 2)
        self.assertEqual(self.runner.calls, 1)
        self.assertIsNone(self.bridge.slot)

    async def test_noop_refresh_does_not_disable_later_manual_refresh(self):
        original = self.api.edit
        failed = False
        async def edit(*args, **kwargs):
            nonlocal failed
            if "📖 阅读中" in args[2] and not failed:
                failed = True
                raise TelegramError(400, not_modified=True)
            return await original(*args, **kwargs)
        self.api.edit = edit
        job = await self.held_job()
        await self.action(job, "expand")
        await self.until(lambda: failed)
        job.execution_steps.append("✅ refreshed after noop")
        await self.action(job, "refresh")
        await self.until(lambda: "refreshed after noop" in self.card(job)[1])
        self.assertTrue(job.progress_expanded)

    async def test_reading_request_during_final_delivery_is_ignored(self):
        job = await self.held_job()
        job.phase = "sending"
        await self.action(job, "expand")
        self.assertFalse(job.progress_expanded)
        self.assertFalse(job.progress_requested.is_set())
        self.assertIn("已结束", self.api.answered_callbacks[-1][1])
