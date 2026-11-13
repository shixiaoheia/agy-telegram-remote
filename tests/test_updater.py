import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

try:
    from .common import ROOT, settings_at
    from .test_bot import FakeAPI, FakeRunner, update
except ImportError:
    from common import ROOT, settings_at
    from test_bot import FakeAPI, FakeRunner, update
import updater
from bot import Bridge, Job
from state_store import Store, atomic_json


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.settings = settings_at(self.base)
        self.releases = self.base / "releases"
        self.releases.mkdir()
        self.app = self.base / "app"
        self.old, self.new = "a" * 40, "b" * 40
        self.before = self.release(self.old)
        self.after = self.release(self.new)
        self.app.symlink_to(self.before)
        self.request_id = "c" * 32
        atomic_json(self.settings.state_dir / "update-request.json", {
            "request_id": self.request_id, "created_at": time.time(), "language": "en"})
        self.trust = patch("updater.trusted")
        self.trust.start()
        self.notification = patch("updater.notify", new_callable=AsyncMock)
        self.sent = self.notification.start()

    def tearDown(self):
        self.notification.stop()
        self.trust.stop()
        self.temp.cleanup()

    def release(self, commit):
        path = self.releases / commit
        path.mkdir()
        for name in ("install.sh", "updater.py"):
            (path / name).write_text("fixture")
        (path / ".commit").write_text(commit + "\n")
        return path

    def apply(self):
        return updater.apply(self.settings, app=self.app, releases=self.releases)

    def test_already_current_never_runs_installer_or_restarts(self):
        with patch("updater.latest_revision", return_value=self.old), patch("updater.install_revision") as install:
            self.assertEqual(self.apply(), 0)
        install.assert_not_called()
        self.assertEqual(updater.read_status(self.settings.state_dir)["phase"], "current")
        self.sent.assert_awaited_once()

    def test_success_requires_target_revision_and_healthy_new_bot(self):
        def switch(*args):
            self.app.unlink()
            self.app.symlink_to(self.after)
            return 0
        with patch("updater.latest_revision", return_value=self.new), patch("updater.install_revision", side_effect=switch) as install, patch("updater.healthy", return_value=True):
            self.assertEqual(self.apply(), 0)
        self.assertEqual(install.call_args.args[:2], (self.before, self.new))
        self.assertEqual(updater.read_status(self.settings.state_dir)["phase"], "success")
        self.assertEqual(self.sent.call_args.args[0].owner_id, self.settings.owner_id)

    def test_installer_failure_reports_verified_original_version(self):
        with patch("updater.latest_revision", return_value=self.new), patch("updater.install_revision", return_value=1), patch("updater.healthy", return_value=True):
            self.assertEqual(self.apply(), 1)
        self.assertEqual(updater.read_status(self.settings.state_dir)["phase"], "rolled_back")

    def test_unhealthy_or_wrong_revision_is_never_success(self):
        with patch("updater.latest_revision", return_value=self.new), patch("updater.install_revision", return_value=0), patch("updater.healthy", return_value=False):
            self.assertEqual(self.apply(), 1)
        self.assertEqual(updater.read_status(self.settings.state_dir)["phase"], "failed")

    def test_network_failure_keeps_original_and_redacts_raw_diagnostics(self):
        with patch("updater.latest_revision", side_effect=ValueError("private-key-and-url")), patch("updater.healthy", return_value=True), patch("updater.install_revision") as install:
            self.assertEqual(self.apply(), 1)
        install.assert_not_called()
        status = updater.read_status(self.settings.state_dir)
        self.assertEqual(status["phase"], "retained")
        self.assertNotIn("private-key-and-url", str(status))

    def test_completed_request_is_not_replayed(self):
        with patch("updater.latest_revision", return_value=self.old) as latest:
            self.apply()
            self.apply()
        latest.assert_called_once()
        self.sent.assert_awaited_once()

    def test_notification_failure_does_not_erase_or_replay_completed_update(self):
        self.sent.side_effect = OSError("offline")
        with patch("updater.latest_revision", return_value=self.old) as latest:
            self.assertEqual(self.apply(), 0)
            self.assertEqual(self.apply(), 0)
        latest.assert_called_once()
        self.assertEqual(updater.read_status(self.settings.state_dir)["phase"], "current")

    def test_expired_or_malformed_request_never_fetches_code(self):
        for nonce, stamp in ((self.request_id, time.time() - 901), ("bad", time.time()), (self.request_id, float("inf"))):
            # JSON itself rejects non-finite timestamps; use a future finite one.
            stamp = time.time() + 10 if stamp == float("inf") else stamp
            atomic_json(self.settings.state_dir / "update-request.json", {"request_id": nonce, "created_at": stamp})
            with patch("updater.latest_revision") as latest, self.assertRaises(ValueError):
                self.apply()
            latest.assert_not_called()

    def test_ref_resolution_is_fixed_to_main_and_rejects_extra_output(self):
        result = subprocess.CompletedProcess([], 0, stdout=(self.new + "\trefs/heads/main\n").encode())
        with patch("updater.subprocess.run", return_value=result) as run:
            self.assertEqual(updater.latest_revision({}), self.new)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], [updater.REPO, "refs/heads/main"])
        result.stdout += b"other-line\n"
        with patch("updater.subprocess.run", return_value=result), self.assertRaises(ValueError):
            updater.latest_revision({})

    def test_installer_is_called_without_shell_or_credentials_and_only_pinned_sha(self):
        process = Mock(returncode=0)
        process.wait.return_value = 0
        with patch("updater.subprocess.Popen", return_value=process) as spawn:
            self.assertEqual(updater.install_revision(self.before, self.new, {}), 0)
        self.assertEqual(spawn.call_args.args[0], ["/bin/bash", str(self.before / "install.sh"), "--bot-update", "--ref", self.new])
        self.assertNotIn("shell", spawn.call_args.kwargs)
        with self.assertRaises(ValueError):
            updater.install_revision(self.before, "main;bad", {})

    def test_timeout_terminates_installer_group_before_return(self):
        process = Mock(pid=123, returncode=None)
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 900), 143]
        with patch("updater.subprocess.Popen", return_value=process), patch("updater.os.killpg") as kill, self.assertRaises(subprocess.TimeoutExpired):
            updater.install_revision(self.before, self.new, {})
        kill.assert_called_once_with(123, updater.signal.SIGTERM)

    def test_external_release_and_symlinked_marker_are_rejected(self):
        self.app.unlink()
        self.app.symlink_to(self.base)
        with self.assertRaises(ValueError):
            updater.current_revision(self.app, self.releases)
        self.trust.stop()
        try:
            marker = self.base / "linked"
            marker.symlink_to(self.after / ".commit")
            with self.assertRaises((ValueError, OSError)):
                updater.trusted(marker)
        finally:
            self.trust.start()

    def test_worker_environment_excludes_bot_keys_and_user_git_configuration(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "not-a-real-token", "PYTHONPATH": "/untrusted", "GIT_CONFIG_GLOBAL": "/bad"}):
            env = updater.worker_environment(self.settings)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_updater_has_an_independent_service_and_no_model_execution(self):
        unit = updater.unit_text()
        self.assertIn("Type=oneshot", unit)
        self.assertIn("updater.py apply", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertNotIn("PartOf=", unit)
        self.assertNotIn("BOT_TOKEN", unit)
        self.assertNotIn("--dangerously", unit)

    def test_installer_rollback_restores_or_removes_update_unit_transactionally(self):
        for existing in (True, False):
            backup = self.base / ("backup-old" if existing else "backup-new")
            backup.mkdir()
            unit = self.base / ("unit-old" if existing else "unit-new")
            unit.write_text("new unit")
            if existing:
                (backup / "update-service").write_text("old unit")
            script = '''
source "$1"
BACKUP="$2"; UPDATE_UNIT="$3"; UPDATE_UNIT_CHANGED=1
systemctl() { return 0; }
report_service_state() { :; }
rollback
'''
            subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh"), str(backup), str(unit)],
                           check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(unit.read_text() if unit.exists() else None, "old unit" if existing else None)

    def test_installer_rejects_linked_update_unit_before_executing_source(self):
        target = self.base / "foreign"
        target.write_text("do not change")
        link = self.base / "unit-link"
        link.symlink_to(target)
        result = subprocess.run(["bash", "-c", 'source "$1"; UPDATE_UNIT="$2"; BACKUP="$3"; install_update_unit "$3"',
                                 "_", str(ROOT / "install.sh"), str(link), str(self.base)],
                                timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "do not change")


class BotUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed, self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        if self.bridge.restart_task:
            self.bridge.restart_task.cancel()
            await asyncio.gather(self.bridge.restart_task, return_exceptions=True)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

    async def command(self, text, **kwargs):
        await self.bridge.handle(update(text, **kwargs))
        if self.bridge.reply_worker:
            await self.bridge.reply_worker

    async def test_only_owner_private_chat_can_request_or_read_updates(self):
        with patch("bot.update_systemctl", new_callable=AsyncMock) as ctl:
            for command in ("/update", "/update status"):
                await self.command(command, user=67890)
                await self.command(command, user=99999)
                await self.command(command, chat_type="group")
            ctl.assert_not_awaited()
        self.assertIsNone(self.bridge.restart_task)

    async def test_running_tasks_waiting_queue_and_unsaved_results_block_update(self):
        self.bridge.slot = Job(12345, 12345)
        await self.command("/update")
        self.assertIsNone(self.bridge.restart_task)
        self.bridge.slot = None
        self.bridge.waiting_jobs.append((12345, 12345, "waiting"))
        await self.command("/update")
        self.assertIsNone(self.bridge.restart_task)
        self.bridge.waiting_jobs.clear()
        self.bridge.unsaved_results[12345] = {"text": "unsaved"}
        await self.command("/update")
        self.assertIsNone(self.bridge.restart_task)

    async def test_arguments_cannot_override_update_source_or_run_shell_commands(self):
        for command in ("/update other/repo", "/update main;reboot", "/update --reauth"):
            await self.command(command)
            self.assertIsNone(self.bridge.restart_task)

    async def test_missing_unit_reports_one_time_install_without_disrupting_bot(self):
        with patch("bot.update_systemctl", new=AsyncMock(side_effect=[3, 4])) as ctl:
            await self.bridge.handle(update("/update"))
            task = self.bridge.restart_task
            await task
        self.assertEqual(ctl.await_args_list[1].args, ("start", "--no-block", updater.SERVICE))
        self.assertEqual(updater.read_status(self.settings.state_dir)["phase"], "unavailable")
        self.assertFalse(self.runner.blocked)

    async def test_update_reserves_slot_and_duplicate_command_does_not_start_another(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def launch(*args):
            entered.set()
            await release.wait()
            return 3
        with patch("bot.update_systemctl", new=launch):
            await self.bridge.handle(update("/update"))
            task = self.bridge.restart_task
            await entered.wait()
            await self.command("/update")
            self.assertIs(self.bridge.restart_task, task)
            await self.command("a task")
            self.assertIsNone(self.bridge.slot)
            release.set()
            await task
        self.assertEqual(self.runner.calls, 0)

    async def test_new_bot_waits_for_updater_readiness_result(self):
        atomic_json(self.settings.state_dir / "update-status.json", {
            "request_id": "d" * 32, "phase": "installing", "updated_at": time.time()})
        self.assertTrue(self.bridge._restart_pending())
        await self.command("a task")
        self.assertIsNone(self.bridge.slot)
        atomic_json(self.settings.state_dir / "update-status.json", {
            "request_id": "d" * 32, "phase": "success", "updated_at": time.time()})
        self.assertFalse(self.bridge._restart_pending())

    async def test_status_retrieves_durable_result_without_installing(self):
        atomic_json(self.settings.state_dir / "update-status.json", {
            "request_id": "d" * 32, "phase": "success", "actual": "a" * 40})
        with patch("bot.update_systemctl", new_callable=AsyncMock) as ctl:
            await self.command("/update status")
        ctl.assert_not_awaited()
        self.assertIn("更新完成", self.api.messages[-1][1])
