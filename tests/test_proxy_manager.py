"""Proxy activation tests use fakes; they never invoke real systemd."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import proxy_manager as manager


class ProxyManagerTests(unittest.TestCase):
    @staticmethod
    def old_snapshot(port=32123):
        return {manager.CONFIG: json.dumps({"inbounds": [{"type": "http", "listen": "127.0.0.1",
                                                             "listen_port": port}]}).encode(),
                manager.ENV: manager.env_text(port), manager.UNIT: manager.MARKER.encode() + b"old-unit",
                manager.DROP: manager.MARKER.encode() + b"old-drop"}

    @staticmethod
    def new_config(port=32123):
        return {"inbounds": [{"type": "http", "listen": "127.0.0.1", "listen_port": port}],
                "outbounds": []}

    def test_ready_uses_trusted_manager_release(self):
        show = subprocess.CompletedProcess([], 0, stdout="123\n")
        with patch.object(manager.subprocess, "run", return_value=show), \
             patch.object(manager, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(manager.ready())
        self.assertEqual(run.call_args.args[4], str(Path(manager.__file__).resolve().with_name("manage.py")))

    def test_service_environment_is_private_and_loopback_only(self):
        env = manager.env_text(32123).decode()
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:32123", env)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,::1", env)
        self.assertIn(f"EnvironmentFile={manager.ENV}", manager.drop_text().decode())
        self.assertIn("NoNewPrivileges=yes", manager.unit_text().decode())
        self.assertIn(f"Requires={manager.PROXY_SERVICE}", manager.drop_text().decode())

    def test_bot_rejects_missing_environment_or_listener_for_managed_proxy(self):
        files = self.old_snapshot()
        good = {k: "http://127.0.0.1:32123" for k in
                ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
        good.update({"NO_PROXY": "localhost,127.0.0.1,::1",
                     "no_proxy": "localhost,127.0.0.1,::1", "ALL_PROXY": "", "all_proxy": ""})
        with patch.object(manager, "read_managed", side_effect=lambda p: files.get(p)), \
             patch.object(manager, "wait_listener") as listener:
            with self.assertRaises(manager.ProxyError):
                manager.check_bot_proxy_environment({})
            listener.assert_not_called()
            with self.assertRaises(manager.ProxyError):
                manager.check_bot_proxy_environment(dict(good, NO_PROXY="*"))
            listener.assert_not_called()
            listener.side_effect = manager.ProxyError("listener unavailable")
            with self.assertRaises(manager.ProxyError):
                manager.check_bot_proxy_environment(good)
            listener.side_effect = None
            manager.check_bot_proxy_environment(good)
            listener.assert_called_with(32123, timeout=10)

    def test_direct_install_is_not_forced_to_use_project_proxy(self):
        with patch.object(manager, "read_managed", return_value=None), \
             patch.object(manager, "wait_listener") as listener:
            manager.check_bot_proxy_environment({})
            listener.assert_not_called()

    def test_incomplete_project_proxy_blocks_bot_start(self):
        with patch.object(manager, "read_managed",
                          side_effect=lambda p: b"partial" if p == manager.CONFIG else None):
            with self.assertRaises(manager.ProxyError):
                manager.check_bot_proxy_environment({})

    def test_interrupted_first_switch_without_proxy_files_blocks_start(self):
        with patch.object(manager, "read_managed",
                          side_effect=lambda p: b"pending" if p == manager.JOURNAL else None):
            with self.assertRaises(manager.ProxyError):
                manager.check_bot_proxy_environment({})

    def test_connection_probe_separates_network_failure_and_http_status(self):
        with patch.object(manager.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, stdout="403"),
                subprocess.CompletedProcess([], 0, stdout="200")]):
            manager.check_connectivity(32123)
        with patch.object(manager.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 7, stdout="000")):
            with self.assertRaisesRegex(manager.ProxyError, "Google"):
                manager.check_connectivity(32123)

    def test_candidate_telegram_check_uses_candidate_proxy_without_credentials_in_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.env"
            config.write_text("fixture", encoding="utf-8")
            with patch.object(manager, "CONFIG_DIR", Path(directory)), \
                 patch.object(manager.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], 0, stdout=b"TELEGRAM_CHECK_OK\n")) as run:
                self.assertTrue(manager.check_telegram_identity(32123))
            args, kwargs = run.call_args
            self.assertIn("check-token", args[0])
            self.assertNotIn("fixture", args[0])
            self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:32123")
            self.assertEqual(kwargs["env"]["ALL_PROXY"], "")

    def test_candidate_telegram_check_skips_missing_configuration(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(manager, "CONFIG_DIR", Path(directory)), \
             patch.object(manager.subprocess, "run") as run:
            self.assertIsNone(manager.check_telegram_identity(32123))
            run.assert_not_called()

    def test_successful_switch_starts_proxy_and_checks_bot_ready(self):
        old = self.old_snapshot()
        files = dict(old)
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        with patch.object(manager, "secure_dir"), \
             patch.object(manager, "read_managed", side_effect=lambda path, marker=False: old[path]), \
             patch.object(manager, "atomic_write", side_effect=lambda path, data, mode: files.update({path: data})), \
             patch.object(manager, "active", return_value=True), \
             patch.object(manager, "enabled", return_value=True), \
             patch.object(manager, "save_transaction"), \
             patch.object(manager, "clear_transaction"), \
             patch.object(manager, "run", side_effect=fake_run), \
             patch.object(manager, "check_connectivity") as check, \
             patch.object(manager, "wait_listener") as listener, \
             patch.object(manager, "ready", return_value=True), \
             patch.object(manager, "service_proxy_matches", return_value=True):
            manager.apply(self.new_config(), 32123)
        self.assertIn(("systemctl", "enable", "--now", manager.PROXY_SERVICE), calls)
        self.assertIn(("systemctl", "start", manager.SERVICE), calls)
        self.assertIn(b"127.0.0.1:32123", files[manager.ENV])
        check.assert_called_once_with(32123)
        listener.assert_called_once_with(32123)

    def test_switch_keeps_old_local_port_after_candidate_probe(self):
        old = self.old_snapshot(32123)
        files = {}
        def active(name):
            return name == manager.PROXY_SERVICE
        with patch.object(manager, "secure_dir"), \
             patch.object(manager, "read_managed", side_effect=lambda path, marker=False: old[path]), \
             patch.object(manager, "atomic_write", side_effect=lambda path, data, mode: files.update({path: data})), \
             patch.object(manager, "save_transaction"), patch.object(manager, "clear_transaction"), \
             patch.object(manager, "active", side_effect=active), patch.object(manager, "enabled", return_value=True), \
             patch.object(manager, "run", side_effect=lambda *a, **k: subprocess.CompletedProcess(a, 0)), \
             patch.object(manager, "wait_listener"), patch.object(manager, "check_connectivity") as check:
            manager.apply(self.new_config(54321), 54321)
        self.assertEqual(json.loads(files[manager.CONFIG])["inbounds"][0]["listen_port"], 32123)
        self.assertEqual(files[manager.ENV], old[manager.ENV])
        check.assert_called_once_with(32123)

    def test_inactive_bot_starts_only_with_explicit_choice(self):
        for answer, expected_calls in (("n", 0), ("y", 1)):
            with self.subTest(answer=answer), \
                 patch.object(manager.os, "geteuid", return_value=0, create=True), \
                 patch.object(manager, "secure_dir"), patch.object(manager.signal, "signal"), \
                 patch.object(manager, "recover_pending"), \
                 patch.object(manager.getpass, "getpass", return_value="anytls://dummy@example.test:443"), \
                 patch.object(manager, "install_engine"), patch.object(manager, "ephemeral_port", return_value=32123), \
                 patch.object(manager, "probe"), patch.object(manager, "apply"), \
                 patch.object(manager, "service_installed", return_value=True), \
                 patch.object(manager, "active", return_value=False), \
                 patch.object(manager, "start_saved_bot") as start, \
                 patch("builtins.input", return_value=answer):
                self.assertEqual(manager.main(), 0)
                self.assertEqual(start.call_count, expected_calls)

    def test_failed_new_proxy_restores_previous_files_and_services(self):
        old = self.old_snapshot()
        files = dict(old)
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            if args == ("systemctl", "enable", "--now", manager.PROXY_SERVICE):
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)

        def fake_write(path, data, mode):
            files[path] = data

        with patch.object(manager, "secure_dir"), \
             patch.object(manager, "read_managed", side_effect=lambda path, marker=False: old[path]), \
             patch.object(manager, "atomic_write", side_effect=fake_write), \
             patch.object(manager, "active", return_value=True), \
             patch.object(manager, "enabled", return_value=True), \
             patch.object(manager, "save_transaction"), \
             patch.object(manager, "clear_transaction"), \
             patch.object(manager, "wait_listener"), \
             patch.object(manager, "wait_bot"), \
             patch.object(manager, "run", side_effect=fake_run):
            with self.assertRaises(subprocess.CalledProcessError):
                manager.apply(self.new_config(), 32123)
        self.assertEqual(files, old)
        self.assertIn(("systemctl", "start", manager.PROXY_SERVICE), calls)
        self.assertIn(("systemctl", "start", manager.SERVICE), calls)

    def test_rejects_conflicting_existing_service_before_stopping_bot(self):
        old = self.old_snapshot()
        def existing(path, marker=False):
            return None if path == manager.UNIT else old[path]

        with patch.object(manager, "secure_dir"), \
             patch.object(manager, "read_managed", side_effect=existing), \
             patch.object(manager, "active", return_value=True), \
             patch.object(manager, "enabled", return_value=True), \
             patch.object(manager, "run") as run:
            with self.assertRaises(manager.ProxyError):
                manager.apply(self.new_config(), 32123)
        run.assert_not_called()

    def test_unready_bot_restores_old_configuration(self):
        old = self.old_snapshot()
        files = dict(old)
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        with patch.object(manager, "secure_dir"), \
             patch.object(manager, "read_managed", side_effect=lambda path, marker=False: old[path]), \
             patch.object(manager, "atomic_write", side_effect=lambda path, data, mode: files.update({path: data})), \
             patch.object(manager, "active", return_value=True), \
             patch.object(manager, "enabled", return_value=True), \
             patch.object(manager, "save_transaction"), \
             patch.object(manager, "clear_transaction"), \
             patch.object(manager, "run", side_effect=fake_run), \
             patch.object(manager, "check_connectivity"), \
             patch.object(manager, "wait_listener"), \
             patch.object(manager, "ready", return_value=False), \
             patch.object(manager.time, "sleep"):
            with self.assertRaises(manager.ProxyError):
                manager.apply(self.new_config(), 32123)
        self.assertEqual(files, old)
        self.assertGreaterEqual(calls.count(("systemctl", "start", manager.SERVICE)), 2)

    def test_new_service_disabled_before_rollback_removes_unit(self):
        events = []

        def fake_run(*args, **kwargs):
            events.append(args)
            if args == ("systemctl", "enable", "--now", manager.PROXY_SERVICE):
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)

        def fake_unlink(path, missing_ok=False):
            events.append(("unlink", path))

        with patch.object(manager, "secure_dir"), \
             patch.object(manager, "read_managed", return_value=None), \
             patch.object(manager, "atomic_write"), \
             patch.object(manager, "active", return_value=False), \
             patch.object(manager, "enabled", return_value=False), \
             patch.object(manager, "save_transaction"), \
             patch.object(manager, "clear_transaction"), \
             patch.object(manager, "run", side_effect=fake_run), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "unlink", autospec=True, side_effect=fake_unlink):
            with self.assertRaises(subprocess.CalledProcessError):
                manager.apply(self.new_config(), 32123)
        self.assertLess(events.index(("systemctl", "disable", manager.PROXY_SERVICE)),
                        events.index(("unlink", manager.UNIT)))
