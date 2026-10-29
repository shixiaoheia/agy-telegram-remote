"""Crash recovery tests use isolated temporary files and fake services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import proxy_manager as manager


class CrashRecoveryTests(unittest.TestCase):
    def test_killed_switch_keeps_port_and_can_restore_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "proxy.json"
            env = root / "proxy.env"
            unit = root / "unit"
            drop = root / "drop"
            journal = root / "transaction"
            old_config = json.dumps({"inbounds": [{"type": "http", "listen": "127.0.0.1",
                                                  "listen_port": 12345}], "old_node": True}).encode()
            old_env = manager.env_text(12345)
            old_unit = manager.MARKER.encode() + b"old-unit"
            old_drop = manager.MARKER.encode() + b"old-drop"
            for path, content in ((config, old_config), (env, old_env), (unit, old_unit), (drop, old_drop)):
                path.write_bytes(content)
            child = r'''
import os, pathlib, subprocess, sys
import proxy_manager as m
p = pathlib.Path(sys.argv[1])
m.CONFIG, m.ENV, m.UNIT, m.DROP, m.JOURNAL = [p / name for name in ("proxy.json", "proxy.env", "unit", "drop", "transaction")]
m.CONFIG_DIR = m.DROP_DIR = p
m.secure_dir = lambda *args: None
m.read_managed = lambda path, marker=False: path.read_bytes() if path.exists() else None
m.active = lambda name: False
m.enabled = lambda name: False
m.run = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
def write(path, content, mode):
    if path == m.ENV:
        os._exit(97)
    path.write_bytes(content)
m.atomic_write = write
m.apply({"inbounds": [{"type": "http", "listen": "127.0.0.1", "listen_port": 54321}], "new_node": True}, 54321)
'''
            result = subprocess.run([sys.executable, "-c", child, temp], cwd=ROOT,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 97, result.stderr)
            self.assertTrue(journal.is_file())
            self.assertEqual(json.loads(config.read_bytes())["inbounds"][0]["listen_port"], 12345)
            self.assertEqual(env.read_bytes(), old_env)

            def read(path, marker=False):
                return path.read_bytes() if path.exists() else None

            def write(path, data, mode):
                path.write_bytes(data)

            patches = {"CONFIG": config, "ENV": env, "UNIT": unit, "DROP": drop,
                       "JOURNAL": journal, "CONFIG_DIR": root, "DROP_DIR": root}
            with patch.multiple(manager, **patches), \
                 patch.object(manager, "read_managed", side_effect=read), \
                 patch.object(manager, "atomic_write", side_effect=write), \
                 patch.object(manager, "sync_directory"), \
                 patch.object(manager, "active", return_value=False), \
                 patch.object(manager, "run", side_effect=lambda *a, **k: subprocess.CompletedProcess(a, 0)):
                self.assertTrue(manager.recover_pending())
            self.assertEqual(config.read_bytes(), old_config)
            self.assertEqual(env.read_bytes(), old_env)
            self.assertEqual(unit.read_bytes(), old_unit)
            self.assertEqual(drop.read_bytes(), old_drop)
            self.assertFalse(journal.exists())

    def test_invalid_record_is_not_deleted(self):
        with patch.object(manager, "read_managed", return_value=b'{"version": 99}'), \
             patch.object(manager, "clear_transaction") as clear:
            with self.assertRaises(manager.ProxyError):
                manager.recover_pending()
        clear.assert_not_called()

    def test_failed_restore_retains_record(self):
        with patch.object(manager, "read_transaction", return_value=({}, True, True, True)), \
             patch.object(manager, "restore_transaction", return_value=False), \
             patch.object(manager, "clear_transaction") as clear:
            with self.assertRaises(manager.ProxyError):
                manager.recover_pending()
        clear.assert_not_called()

    def test_rejects_existing_mismatched_ports_before_switch(self):
        config = json.dumps({"inbounds": [{"type": "http", "listen": "127.0.0.1",
                                           "listen_port": 10001}]}).encode()
        before = {manager.CONFIG: config, manager.ENV: manager.env_text(20002)}
        with self.assertRaises(manager.ProxyError):
            manager.previous_port(before)
