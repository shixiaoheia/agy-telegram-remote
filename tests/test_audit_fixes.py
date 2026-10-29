"""Regression tests for queue, recovery, and slow control boundaries."""
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .common import ROOT, settings_at
    from .test_bot import FakeAPI, FakeRunner, callback_update, update
except ImportError:
    from common import ROOT, settings_at
    from test_bot import FakeAPI, FakeRunner, callback_update, update

from agy_runner import Result
from bot import Bridge
from state_store import Store
from telegram_api import TelegramError


class AuditFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed,
                           self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        self.bridge.waiting_jobs.clear()
        self.runner.finish.set()
        if self.bridge.slot and self.bridge.slot.worker:
            self.bridge.slot.cancel.set()
            await asyncio.gather(self.bridge.slot.worker, return_exceptions=True)
        if self.bridge.model_check_task:
            self.bridge._cancel_model_health()
            await asyncio.gather(self.bridge.model_check_task, return_exceptions=True)
        if self.bridge.album_task:
            self.bridge.album_task.cancel()
            await asyncio.gather(self.bridge.album_task, return_exceptions=True)
        if self.bridge.queue_task:
            await asyncio.gather(self.bridge.queue_task, return_exceptions=True)
        if self.bridge.reply_worker:
            await asyncio.gather(self.bridge.reply_worker, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    async def test_queued_job_reserves_workspace_before_model_probe(self):
        self.runner.hold = True
        self.bridge.waiting_jobs.append((12345, 12345, 'first waiting task'))
        self.bridge._wake_queue()
        self.bridge._start_model_health_refresh(12345)
        await asyncio.wait_for(self.runner.started.wait(), 1)
        self.assertIsNone(self.bridge.model_check_task)
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.runner.last_prompt, 'first waiting task')
        self.assertEqual(len(self.bridge.waiting_jobs), 0)

    async def test_album_finishing_wakes_preserved_queue(self):
        self.runner.hold = True
        attachment = {'file_id': 'fixture', 'file_size': 4}
        event = {'message': {'from': {'id': 12345}, 'chat': {'id': 12345, 'type': 'private'},
                             'media_group_id': 'album', 'photo': [attachment]}}
        self.bridge._buffer_album(event, 12345, 'album', attachment, True, '')
        self.bridge.waiting_jobs.append((12345, 12345, 'waiting while album finishes'))
        self.bridge._wake_queue()
        with patch('bot.ALBUM_SETTLE_SECONDS', 0):
            await self.bridge.album_task
        await asyncio.wait_for(self.runner.started.wait(), 1)
        self.assertEqual(self.runner.last_prompt, 'waiting while album finishes')
        self.assertEqual(len(self.bridge.waiting_jobs), 0)

    async def test_queue_start_error_preserves_item_until_owner_resumes(self):
        self.runner.hold = True
        entry = (12345, 12345, 'not yet started')
        self.bridge.waiting_jobs.append(entry)
        with patch.object(self.store, 'get_model', side_effect=OSError('fixture')):
            self.bridge._wake_queue()
            await self.bridge.queue_task
            self.assertEqual(list(self.bridge.waiting_jobs), [entry])
            self.assertTrue(self.bridge.queue_paused)
            await self.bridge.handle(update('/queue resume', user=67890))
            self.assertTrue(self.bridge.queue_paused)
        await self.bridge.handle(update('/queue resume'))
        await asyncio.wait_for(self.runner.started.wait(), 1)
        self.assertEqual(self.runner.last_prompt, entry[2])
        self.assertFalse(self.bridge.waiting_jobs)

    async def test_result_save_failure_recovers_before_last_and_never_reruns(self):
        self.runner.hold = True
        self.runner.result = Result('success', text='valuable offline result')
        await self.bridge.handle(update('one operation'))
        await self.runner.started.wait()
        job = self.bridge.slot
        with patch.object(self.store, 'save', side_effect=OSError('fixture')):
            self.runner.finish.set()
            await job.worker
            self.assertIn(12345, self.bridge.unsaved_results)
            self.assertFalse(self.runner.blocked)
            self.assertFalse(any('valuable offline result' in m[1] for m in self.api.messages))
            await self.bridge.handle(update('/last', user=67890))
            if self.bridge.reply_worker:
                await self.bridge.reply_worker
            self.assertNotIn('valuable offline result', self.api.messages[-1][1])
            await self.bridge.handle(update('/last'))
            if self.bridge.reply_worker:
                await self.bridge.reply_worker
            self.assertIn('无法保存', self.api.messages[-1][1])
            await self.bridge.handle(update('another operation'))
            self.assertEqual(self.runner.calls, 1)
        await self.bridge.handle(update('/last'))
        await self.bridge.reply_worker
        self.assertEqual(self.store.load(12345)['text'], 'valuable offline result')
        self.assertIn('valuable offline result', self.api.messages[-1][1])
        self.assertNotIn(12345, self.bridge.unsaved_results)
        self.assertEqual(self.runner.calls, 1)

    async def test_history_failure_is_recovered_before_new_task_overwrites_last(self):
        self.runner.hold = True
        self.runner.result = Result('success', text='durable but no history')
        await self.bridge.handle(update('first operation'))
        await self.runner.started.wait()
        job = self.bridge.slot
        with patch.object(self.store, 'save_history', side_effect=OSError('fixture')):
            self.runner.finish.set()
            await job.worker
            self.assertEqual(self.store.load(12345)['text'], 'durable but no history')
            self.assertIn(12345, self.bridge.unsaved_results)
            await self.bridge.handle(update('second operation'))
            self.assertEqual(self.runner.calls, 1)
        await self.bridge.handle(update('/last'))
        await self.bridge.reply_worker
        self.assertEqual(self.store.history_item(12345, job.job_id)['text'], 'durable but no history')
        self.assertNotIn(12345, self.bridge.unsaved_results)

    async def test_failed_result_save_prevents_steer_handoff_from_overwriting_it(self):
        self.runner.hold = True
        await self.bridge.handle(update('original operation'))
        await self.runner.started.wait()
        original = self.bridge.slot
        with patch.object(self.store, 'save', side_effect=OSError('fixture')):
            await self.bridge.handle(update('/steer updated operation'))
            handoff = original.steer_task
            await asyncio.wait_for(handoff, 2)
            self.assertEqual(self.runner.calls, 1)
            self.assertIn(12345, self.bridge.unsaved_results)
            self.assertIsNone(self.bridge.slot)

    async def test_capacity_error_after_tool_never_resumes_same_conversation(self):
        self.store.set_model(12345, 'gemini-3.8-flash-high')
        calls = []
        async def run(prompt, cancel, **kwargs):
            calls.append(prompt)
            await kwargs['progress']('正在调用工具：fixture')
            return Result('error', category='capacity', conversation_id='example-conversation')
        self.runner.run = run
        await self.bridge.handle(update('perform operation'))
        job = self.bridge.slot
        await job.worker
        self.assertTrue(job.tool_started)
        self.assertEqual(calls, ['perform operation'])
        self.assertEqual(self.store.load(12345)['category'], 'capacity')

    async def test_slow_selector_edit_does_not_delay_cancel(self):
        self.runner.hold = True
        await self.bridge.handle(update('held task'))
        await self.runner.started.wait()
        job = self.bridge.slot
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_edit(*args, **kwargs):
            entered.set()
            await release.wait()
            return True
        self.api.edit = slow_edit
        callback = callback_update('status:summary')
        callback['update_id'] = 1
        batch = asyncio.create_task(self.bridge.consume_updates([callback, update('/cancel', update_id=2)]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(batch, 1)
            self.assertTrue(job.cancel.is_set())
        finally:
            release.set()
            await asyncio.gather(batch, return_exceptions=True)
            await job.worker

    async def test_selector_edits_to_same_card_finish_in_order(self):
        entered, release = asyncio.Event(), asyncio.Event()
        edits = []
        async def slow_edit(chat, mid, text, **kwargs):
            edits.append(text)
            if len(edits) == 1:
                entered.set()
                await release.wait()
        self.api.edit = slow_edit
        self.bridge._status_card = lambda user, details: ('details' if details else 'summary', {})
        await self.bridge.handle(callback_update('status:details'))
        await entered.wait()
        await self.bridge.handle(callback_update('status:summary'))
        release.set()
        await self.bridge.reply_worker
        self.assertEqual(edits, ['details', 'summary'])

    async def test_failed_selector_fallback_precedes_newer_edit(self):
        actions = []
        async def edit(chat, mid, text, **kwargs):
            actions.append('edit:' + text)
            if text == 'details':
                raise TelegramError(400)
        async def send(chat, text, **kwargs):
            actions.append('fallback:' + text)
            return {'message_id': 11}
        self.api.edit = edit
        self.api.send = send
        self.bridge._status_card = lambda user, details: ('details' if details else 'summary', {})
        await self.bridge.handle(callback_update('status:details'))
        await self.bridge.handle(callback_update('status:summary'))
        await self.bridge.reply_worker
        self.assertEqual(actions, ['edit:details', 'fallback:details', 'edit:summary'])


class ReleaseValidationTests(unittest.TestCase):
    def test_old_and_new_release_require_only_their_own_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('bot.py', 'settings.py', 'state_store.py', 'telegram_api.py',
                         'telegram_formatting.py', 'agy_runner.py', 'manage.py'):
                (root / name).write_text('')
            script = 'source "$1"; validate_release_files "$2"'
            def check():
                return subprocess.run(['bash', '-c', script, '_', str(ROOT / 'install.sh'), str(root)],
                                      capture_output=True, text=True)
            self.assertEqual(check().returncode, 0)
            (root / 'bot.py').write_text('from command_handlers import CommandHandlers\n')
            self.assertNotEqual(check().returncode, 0)
            (root / 'command_handlers.py').write_text('')
            self.assertEqual(check().returncode, 0)
