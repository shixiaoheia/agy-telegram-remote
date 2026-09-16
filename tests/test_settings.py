import os
import tempfile
import unittest
from pathlib import Path

try:
    from .common import config_values
except ImportError:
    from common import config_values
from settings import (ConfigError, Settings, check_no_symlink, merged_config,
                      parse_env, read_private_text, serialize_env)

class ConfigurationTests(unittest.TestCase):
    def test_write_paths_are_explicit_preserved_and_deduplicated(self):
        self.assertEqual(Settings.from_mapping(config_values()).write_paths, ())
        old = config_values() | {"AGY_WRITE_PATHS": "/etc/nginx,/opt/my-app,/etc/nginx"}
        settings = Settings.from_mapping(merged_config(old, "", "", "/root"))
        self.assertEqual(settings.write_paths, (Path("/etc/nginx"), Path("/opt/my-app")))
        self.assertEqual(Settings.from_mapping(old | {"AGY_WRITE_PATHS": ""}).write_paths, ())

    def test_write_paths_reject_broad_grants_and_unit_injection(self):
        for value in ("/", "/etc", "/opt", "/etc/..", "/etc/nginx,", "relative/path",
                      "/etc/nginx /etc/ssh", "/etc/%h", "/etc/nginx\nUser=other",
                      "-/etc/nginx", "/etc/nginx;cmd", ",".join(["/etc/nginx"] * 17)):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Settings.from_mapping(config_values() | {"AGY_WRITE_PATHS": value})

    def test_unknown_permission_key_rejected(self):
        with self.assertRaises(ConfigError):
            Settings.from_mapping(config_values() | {"AGY_SKIP_PERMISSION": "false"})

    def test_load_rejects_unknown_key_without_exposing_value(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config"
            path.write_text(
                "".join(f"{key}={value}\n" for key, value in config_values().items())
                + "AGY_SKIP_PERMISSION=private-value\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                Settings.load(path)
            self.assertNotIn("private-value", str(caught.exception))

    def test_valid_and_invalid_model_setting(self):
        s = Settings.from_mapping(config_values() | {"AGY_MODEL": "gemini-3.1-pro-high"})
        self.assertEqual(s.model, "gemini-3.1-pro-high")
        s2 = Settings.from_mapping(config_values() | {"AGY_MODEL": "3.8"})
        self.assertEqual(s2.model, "gemini-3.8-flash-high")
        s3 = Settings.from_mapping(config_values() | {"AGY_MODEL": "opus"})
        self.assertEqual(s3.model, "claude-opus-4-6-thinking")
        with self.assertRaises(ConfigError):
            Settings.from_mapping(config_values() | {"AGY_MODEL": "bad;injection"})

    def test_defaults_auto_approve(self):
        self.assertTrue(Settings.from_mapping(config_values()).skip_permissions)

    def test_upgrade_preserves_safe_default_when_key_missing(self):
        values = merged_config(config_values(), "", "", "/root")
        self.assertEqual(values["AGY_SKIP_PERMISSIONS"], "false")

    def test_upgrade_preserves_existing_values(self):
        old = config_values() | {
            "AGY_WORKSPACE": "/root/project", "AGY_TIMEOUT_SECONDS": "123",
            "AGY_SKIP_PERMISSIONS": "false", "MAX_REPLY_CHARS": "4500",
        }
        settings = Settings.from_mapping(merged_config(old, "", "", "/root"))
        self.assertFalse(settings.skip_permissions)
        self.assertEqual(settings.timeout, 123)
        self.assertEqual(settings.max_reply, 4500)
        self.assertEqual(str(settings.workspace), "/root/project")

    def test_explicit_upgrade_opt_in(self):
        old = config_values() | {"AGY_SKIP_PERMISSIONS": "false"}
        new = merged_config(old, "", "", "/root", enable_auto=True)
        self.assertTrue(Settings.from_mapping(new).skip_permissions)

    def test_no_shell_expansion(self):
        text = "X=$(touch /tmp/DO_NOT_CREATE)\nY='${HOME}'\n"
        values = parse_env(text)
        self.assertEqual(values["X"], "$(touch /tmp/DO_NOT_CREATE)")
        self.assertEqual(values["Y"], "${HOME}")

    def test_quoted_legacy_config(self):
        values = parse_env("export ALLOWED_USER_IDS='12345,67890' # hi\nAGY_SKIP_PERMISSIONS=false\n")
        self.assertEqual(values["ALLOWED_USER_IDS"], "12345,67890")

    def test_duplicate_config_rejected(self):
        with self.assertRaises(ConfigError):
            parse_env("AGY_SKIP_PERMISSIONS=false\nAGY_SKIP_PERMISSIONS=true")

    def test_invalid_values(self):
        invalid = [
            {"ALLOWED_USER_IDS": ""}, {"ALLOWED_USER_IDS": "0"},
            {"ALLOWED_USER_IDS": "-2"}, {"ALLOWED_USER_IDS": "nan"},
            {"ALLOWED_USER_IDS": "9007199254740992"},
            {"AGY_SKIP_PERMISSIONS": "maybe"},
            {"AGY_TIMEOUT_SECONDS": "-1"}, {"MAX_OUTPUT_BYTES": "999999999"},
            {"AGY_WORKSPACE": "/"}, {"AGY_WORKSPACE": "/etc"},
            {"AGY_WORKSPACE": "/root/../secret"},
            {"AGY_WORKSPACE": "/root/%h"},
            {"AGY_WORKSPACE": "/srv/agy-workspace"},
            {"STATE_DIR": "/etc"}, {"AGY_HOME": "/root\nInject=1"},
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ConfigError):
                Settings.from_mapping(config_values() | item)

    def test_roundtrip_config(self):
        values = merged_config({}, config_values()["TELEGRAM_BOT_TOKEN"], "12345", "/root")
        self.assertEqual(Settings.from_mapping(values), Settings.from_mapping(parse_env(serialize_env(values))))

    def test_symlink_file_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            (p / "real").write_text("ABC=abc")
            (p / "link").symlink_to(p / "real")
            with self.assertRaises(OSError):
                read_private_text(p / "link")
            with self.assertRaises(ConfigError):
                check_no_symlink(p / "link" / "child")

    def test_nonregular_file_rejected_without_block(self):
        with tempfile.TemporaryDirectory() as temp:
            fifo = Path(temp) / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ConfigError):
                read_private_text(fifo)

    def test_oversized_configuration_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config"
            path.write_text("X=" + "a" * 70000)
            with self.assertRaises(ConfigError):
                read_private_text(path)

    def test_owner_id_first_in_list_not_smallest(self):
        values = config_values() | {"ALLOWED_USER_IDS": "99999,11111,55555"}
        settings = Settings.from_mapping(values)
        self.assertEqual(settings.owner_id, 99999)
        self.assertEqual(settings.allowed, frozenset({11111, 55555, 99999}))

    def test_owner_id_default_fallback(self):
        s = Settings(
            token=config_values()["TELEGRAM_BOT_TOKEN"],
            allowed=frozenset({88888, 22222}),
            agy=Path("/bin/true"),
            home=Path("/root"),
            workspace=Path("/root"),
        )
        self.assertEqual(s.owner_id, 22222)
