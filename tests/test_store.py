import os
import tempfile
import time
import unittest
from pathlib import Path
try:
    from .common import DUMMY_TOKEN
except ImportError:
    from common import DUMMY_TOKEN
from state_store import Store, atomic_json, read_json

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "state"
        self.store = Store(self.directory, frozenset({12345, 67890}), 1000, 7, DUMMY_TOKEN)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_last_result_is_user_scoped(self):
        self.store.save(12345, {"job_id": "test", "outcome": "success", "text": "hi"})
        self.assertEqual(self.store.load(12345)["text"], "hi")
        self.assertIsNone(self.store.load(67890))
        with self.assertRaises(ValueError):
            self.store.load(99999)

    def test_credentials_redacted_and_private_mode(self):
        self.store.save(12345, {"text": DUMMY_TOKEN, "outcome": "success"})
        path = self.directory / "last-12345.json"
        self.assertNotIn(DUMMY_TOKEN, path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_result_size_capped(self):
        result = self.store.save(12345, {"text": "x" * 20000, "outcome": "success"})
        self.assertLessEqual(len(result["text"]), 1000)
        self.assertIn("/last", result["text"])
        self.assertEqual(self.store.load(12345)["text"], result["text"])

    def test_expired_result_removed(self):
        self.store.save(12345, {"text": "old", "outcome": "success"})
        path = self.directory / "last-12345.json"
        value = read_json(path)
        value["updated_at"] = time.time() - 9 * 86400
        atomic_json(path, value)
        self.assertIsNone(self.store.load(12345))
        self.assertFalse(path.exists())

    def test_interrupted_record_not_reexecuted(self):
        self.store.save(12345, {"job_id": "job", "outcome": "running"})
        self.store.recover_interrupted()
        record = self.store.load(12345)
        self.assertEqual(record["outcome"], "interrupted")
        self.assertIn("没有自动重跑", record["detail"])

    def test_removed_whitelist_user_pruned(self):
        self.store.save(12345, {"outcome": "success", "text": "old"})
        reduced = Store(self.directory, frozenset({67890}), 1000, 7)
        reduced.maintain()
        self.assertFalse((self.directory / "last-12345.json").exists())

    def test_user_model_persistence_and_pruning(self):
        self.assertIsNone(self.store.get_model(12345))
        self.store.set_model(12345, "gemini-3.1-pro-high")
        self.assertEqual(self.store.get_model(12345), "gemini-3.1-pro-high")
        self.assertIsNone(self.store.get_model(67890))
        path = self.directory / "model-12345.json"
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            self.store.set_model(12345, "bad model name")
        self.store.set_model(12345, None)
        self.assertIsNone(self.store.get_model(12345))
        self.assertFalse(path.exists())
        self.store.set_model(12345, "claude-sonnet-4-6")
        self.assertTrue(path.exists())
        reduced = Store(self.directory, frozenset({67890}), 1000, 7)
        reduced.maintain()
        self.assertFalse(path.exists())

    def test_duplicate_instance_lock(self):
        self.store.lock()
        second = Store(self.directory, self.store.allowed, 1000, 7)
        with self.assertRaises(BlockingIOError):
            second.lock()

    def test_watermark_persisted(self):
        self.store.save_offset(400)
        second = Store(self.directory, self.store.allowed, 1000, 7)
        self.assertEqual(second.offset(), 400)

    def test_symlink_result_cannot_be_read(self):
        target = Path(self.temp.name) / "target"
        target.write_text("{}")
        (self.directory / "last-12345.json").symlink_to(target)
        with self.assertRaises(OSError):
            self.store.load(12345)

    def test_public_state_directory_rejected(self):
        public = Path(self.temp.name) / "public"
        public.mkdir(mode=0o755)
        # mkdir's mode is filtered by the caller's umask. The installer uses
        # 077, so explicitly make this negative-test fixture public. Do not
        # weaken Store's private-directory check or the installer's umask.
        public.chmod(0o755)
        self.assertEqual(public.stat().st_mode & 0o777, 0o755)
        with self.assertRaises(PermissionError):
            Store(public, self.store.allowed, 1000, 7)
