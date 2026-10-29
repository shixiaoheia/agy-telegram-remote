"""Proxy activation tests use fakes; they never invoke real systemd."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import proxy_manager as manager


class ProxyManagerTests(unittest.TestCase):
    def test_service_environment_is_private_and_loopback_only(self):
        env = manager.env_text(32123).decode()
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:32123", env)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,::1", env)
        self.assertIn(f"EnvironmentFile={manager.ENV}", manager.drop_text().decode())
        self.assertIn("NoNewPrivileges=yes", manager.unit_text().decode())

    def test_successful_switch_starts_proxy_and_checks_bot_ready(self):
        paths = (manager.CONFIG, manager.ENV, manager.UNIT, manager.DROP)
        old = {path: f"old-{index}".encode() for index, path in enumerate(paths)}
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
             patch.object(manager, "run", side_effect=fake_run), \
             patch.object(manager, "check_connectivity") as check, \
             patch.object(manager, "ready", return_value=True), \
             patch.object(manager, "service_proxy_matches", return_value=True):
            manager.apply({"outbounds": []}, 32123)
        self.assertIn(("systemctl", "enable", "--now", manager.PROXY_SERVICE), calls)
        self.assertIn(("systemctl", "start", manager.SERVICE), calls)
        self.assertIn(b"127.0.0.1:32123", files[manager.ENV])
        check.assert_called_once_with(32123)

    def test_failed_new_proxy_restores_previous_files_and_services(self):
        paths = (manager.CONFIG, manager.ENV, manager.UNIT, manager.DROP)
        old = {path: f"old-{index}".encode() for index, path in enumerate(paths)}
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
             patch.object(manager, "run", side_effect=fake_run):
            with self.assertRaises(subprocess.CalledProcessError):
                manager.apply({"outbounds": []}, 32123)
        self.assertEqual(files, old)
        self.assertIn(("systemctl", "start", manager.PROXY_SERVICE), calls)
        self.assertIn(("systemctl", "start", manager.SERVICE), calls)

    def test_rejects_conflicting_existing_service_before_stopping_bot(self):
        def existing(path, marker=False):
            return None if path == manager.UNIT else b"managed"

        with patch.object(manager, "secure_dir"), \
             patch.object(manager, "read_managed", side_effect=existing), \
             patch.object(manager, "active", return_value=True), \
             patch.object(manager, "enabled", return_value=True), \
             patch.object(manager, "run") as run:
            with self.assertRaises(manager.ProxyError):
                manager.apply({}, 32123)
        run.assert_not_called()

    def test_unready_bot_restores_old_configuration(self):
        paths = (manager.CONFIG, manager.ENV, manager.UNIT, manager.DROP)
        old = {path: f"old-{index}".encode() for index, path in enumerate(paths)}
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
             patch.object(manager, "run", side_effect=fake_run), \
             patch.object(manager, "check_connectivity"), \
             patch.object(manager, "ready", return_value=False), \
             patch.object(manager.time, "sleep"):
            with self.assertRaises(manager.ProxyError):
                manager.apply({"outbounds": []}, 32123)
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
             patch.object(manager, "run", side_effect=fake_run), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "unlink", autospec=True, side_effect=fake_unlink):
            with self.assertRaises(subprocess.CalledProcessError):
                manager.apply({"outbounds": []}, 32123)
        self.assertLess(events.index(("systemctl", "disable", manager.PROXY_SERVICE)),
                        events.index(("unlink", manager.UNIT)))
