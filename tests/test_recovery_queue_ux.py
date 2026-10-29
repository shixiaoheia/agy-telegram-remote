"""Recovery, authorization, and status behavior on the queue boundaries."""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .common import settings_at
    from .test_bot import FakeAPI, FakeRunner, callback_update, update
except ImportError:
    from common import settings_at
    from test_bot import FakeAPI, FakeRunner, callback_update, update

from agy_runner import Result
from bot import Bridge, Job
from state_store import Store, atomic_json


class QueueRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed, self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        self.bridge.waiting_jobs.clear()
        self.runner.finish.set()
        if self.bridge.slot and self.bridge.slot.worker:
            self.bridge.slot.cancel.set()
            await asyncio.gather(self.bridge.slot.worker, return_exceptions=True)
        for task in (self.bridge.queue_task, self.bridge.reply_worker):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        if self.bridge.callback_tasks:
            await asyncio.gather(*tuple(self.bridge.callback_tasks), return_exceptions=True)
        self.temp.cleanup()

    async def test_revoking_paused_queue_head_unblocks_other_user(self):
        self.store.add_whitelist(88888)
        self.bridge.waiting_jobs.extend([(88888, 88888, 'removed'), (67890, 67890, 'keep')])
        self.bridge.queue_paused = True
        self.bridge.slot = Job(12345, 12345)
        await self.bridge.handle(update('/whitelist remove 88888'))
        self.assertEqual(list(self.bridge.waiting_jobs), [(67890, 67890, 'keep')])
        self.assertFalse(self.bridge.queue_paused)
        self.assertEqual(self.store.load_queue(self.settings.max_prompt), [(67890, 67890, 'keep')])
        self.bridge.slot = None

    async def test_restart_recovers_waiting_tasks_paused_without_executing(self):
        self.store.save_queue([(12345, 12345, 'do only after confirmation')])
        restored = Bridge(self.settings, self.api, self.store, self.runner)
        self.assertTrue(restored.queue_paused)
        restored._wake_queue()
        await asyncio.sleep(0)
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(restored.waiting_jobs[0][2], 'do only after confirmation')
        self.assertEqual((self.store.directory / 'waiting-queue.json').stat().st_mode & 0o777, 0o600)

    async def test_each_recovered_task_requires_its_owner_to_confirm(self):
        self.store.save_queue([(12345, 12345, 'first'), (67890, 67890, 'second')])
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)
        self.runner.hold = True
        await self.bridge.handle(update('/queue resume'))
        await asyncio.wait_for(self.runner.started.wait(), 1)
        if self.bridge.queue_task:
            await self.bridge.queue_task
        self.assertTrue(self.bridge.queue_paused)
        self.runner.finish.set()
        await self.bridge.slot.worker
        await asyncio.sleep(0)
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(self.bridge.waiting_jobs[0][0], 67890)
        await self.bridge.handle(update('/queue resume'))
        self.assertTrue(self.bridge.queue_paused)

    async def test_queue_button_is_bound_to_owner_and_one_entry(self):
        first, other = (12345, 12345, 'mine'), (67890, 67890, 'private')
        self.bridge.waiting_jobs.extend([first, other])
        self.bridge.queue_paused = True
        self.bridge.slot = Job(12345, 12345)
        await self.bridge.handle(update('/queue'))
        await self.bridge.reply_worker
        message = self.api.messages[-1]
        self.assertIn('mine', message[1])
        self.assertNotIn('private', message[1])
        callback = message[2]['inline_keyboard'][0][0]['callback_data']
        await self.bridge.handle(callback_update(callback, user=67890, chat_id=67890))
        self.assertEqual(len(self.bridge.waiting_jobs), 2)
        await self.bridge.handle(callback_update(callback))
        self.assertEqual(list(self.bridge.waiting_jobs), [other])
        self.bridge.slot = None

    async def test_result_recovers_after_restart_without_rerun(self):
        self.runner.hold = True
        self.runner.result = Result('success', text='recovery payload')
        await self.bridge.handle(update('one operation'))
        await self.runner.started.wait()
        job = self.bridge.slot
        with patch.object(self.store, 'save', side_effect=OSError('fixture')):
            self.runner.finish.set()
            await job.worker
        restored = Bridge(self.settings, self.api, self.store, self.runner)
        self.assertIn(12345, restored.unsaved_results)
        self.assertTrue(restored._recover_unsaved(12345))
        self.assertEqual(self.store.load(12345)['text'], 'recovery payload')
        self.assertIsNone(self.store.load_emergency(self.settings.home, 12345))
        self.assertEqual(self.runner.calls, 1)

    async def test_queue_write_failure_does_not_acknowledge_task(self):
        self.bridge.slot = Job(67890, 67890)
        with patch.object(self.store, 'save_queue', side_effect=OSError('fixture')):
            await self.bridge.handle(update('cannot safely queue'))
        self.assertFalse(self.bridge.waiting_jobs)
        await self.bridge.reply_worker
        self.assertIn('本次没有接收', self.api.messages[-1][1])
        self.bridge.slot = None

    async def test_corrupt_queue_is_not_overwritten_by_cancel(self):
        path = self.store.directory / 'waiting-queue.json'
        path.write_bytes(b'not valid json')
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)
        self.assertFalse(self.bridge.queue_storage_ok)
        await self.bridge.handle(update('/queue cancel'))
        self.assertEqual(path.read_bytes(), b'not valid json')

    async def test_cancel_updates_existing_expanded_progress_card(self):
        job = Job(12345, 12345, history_title='held task')
        job.status_message_id = 1
        job.progress_expanded = True
        self.bridge.slot = job
        task = asyncio.create_task(self.bridge._refresh_progress(job))
        await asyncio.sleep(0)
        await self.bridge.handle(update('/cancel'))
        await asyncio.wait_for(task, 1)
        self.assertTrue(self.api.edits)
        self.assertIn('正在取消并清理', self.api.edits[-1][1])
        self.assertTrue(job.progress_expanded)
        self.bridge.slot = None

    async def test_health_check_accepts_current_poll_and_rejects_stale_or_wrong_pid(self):
        marker = Path(self.temp.name) / 'ready.json'
        def run(pid):
            return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'manage.py'),
                                   'check-health', '--file', str(marker), '--pid', str(pid)],
                                  capture_output=True, check=False).returncode
        record = {'pid': os.getpid(), 'initialized': True, 'polling_ready': True,
                  'last_poll_at': time.time()}
        atomic_json(marker, record)
        self.assertEqual(run(os.getpid()), 0)
        self.assertEqual(run(os.getpid() + 1), 1)
        record['last_poll_at'] = time.time() - 100
        atomic_json(marker, record)
        self.assertEqual(run(os.getpid()), 1)
