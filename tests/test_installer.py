import asyncio
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
try:
    from .common import ROOT, config_values, settings_at
except ImportError:
    from common import ROOT, config_values, settings_at
from agy_runner import Result, SMOKE_PROMPT, build_command
from manage import main, service_unit, smoke
from settings import ConfigError, Settings, parse_env

class InstallerTests(unittest.TestCase):
    def test_help_does_not_install(self):
        result = subprocess.run(["bash", str(ROOT / "install.sh"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--uninstall", result.stdout)

    def test_purge_requires_explicit_uninstall(self):
        result = subprocess.run(["bash", str(ROOT / "install.sh"), "--purge"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--uninstall", result.stderr)

    def test_bash_syntax(self):
        subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)

    def test_supported_os_matrix(self):
        for name, version, supported in [
            ("debian", "12", True), ("debian", "13", True), ("debian", "11", False),
            ("ubuntu", "22.04", True), ("ubuntu", "24.04", True),
            ("ubuntu", "22.01", False), ("ubuntu", "20.04", False),
            ("alpine", "3.20", False), ("debian", "bad", False),
        ]:
            with self.subTest(name=name, version=version):
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; supported_os "$2" "$3"',
                     "_", str(ROOT / "install.sh"), name, version],
                    capture_output=True,
                )
                self.assertEqual(result.returncode == 0, supported)

    def test_service_hardening_and_runtime(self):
        unit = service_unit(Path("/home/agy-tg"), Path("/srv/agy-workspace"),
                            Path("/var/lib/agy-telegram-remote"))
        self.assertIn("User=agy-tg", unit)
        self.assertIn("/usr/bin/python3 -E -s -B", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("RuntimeDirectoryMode=0700", unit)
        self.assertNotIn("/venv/", unit)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", unit)

    def test_no_old_venv_or_dotenv_execution(self):
        script = (ROOT / "install.sh").read_text()
        self.assertNotIn("/venv/bin/python", script)
        self.assertNotIn('source "$CONFIG"', script)
        self.assertNotIn('. "$CONFIG"', script)
        self.assertIn('as_user /usr/bin/python3', script)
        self.assertIn("check-ready", script)
        self.assertIn('CONFIG_CHANGED=1', script)
        self.assertIn("rollback", script)

    def test_only_three_numbered_wizard_steps(self):
        text = (ROOT / "install.sh").read_text() + (ROOT / "manage.py").read_text()
        for number in range(1, 4):
            self.assertEqual(text.count(f"步骤 {number}/3"), 1)
        self.assertNotIn("步骤 4/", text)
        self.assertNotIn("PERMISSION_CONFIRM", text)
        self.assertNotIn("AUTH_START", text)

    def test_prepare_config_prompts_only_two_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "candidate"
            dest.touch()
            argv = ["manage.py", "prepare-config", "--output", str(dest),
                    "--home", "/home/agy-tg"]
            with patch("sys.argv", argv), patch("getpass.getpass", return_value=config_values()["TELEGRAM_BOT_TOKEN"]) as gp, \
                 patch("builtins.input", return_value="12345") as inp, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            gp.assert_called_once()
            inp.assert_called_once()
            self.assertTrue(Settings.load(dest).skip_permissions)

    def test_prepare_config_preserves_old_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            old, dest = Path(temp) / "old", Path(temp) / "candidate"
            old.write_text("\n".join(f"{k}={v}" for k, v in (config_values() | {
                "AGY_WORKSPACE": "/srv/agy-workspace/project",
                "AGY_SKIP_PERMISSIONS": "false", "AGY_TIMEOUT_SECONDS": "321",
            }).items()))
            dest.touch()
            argv = ["manage.py", "prepare-config", "--old", str(old),
                    "--output", str(dest), "--home", "/home/agy-tg"]
            with patch("sys.argv", argv), patch("getpass.getpass", return_value=""), \
                 patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            value = Settings.load(dest)
            self.assertFalse(value.skip_permissions)
            self.assertEqual(value.timeout, 321)
            self.assertEqual(value.workspace, Path("/srv/agy-workspace/project"))

    def test_rollback_restores_files_in_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "old").mkdir()
            (base / "new").mkdir()
            (base / "backup").mkdir()
            (base / "app").symlink_to(base / "new")
            (base / "config").write_text("new config")
            (base / "unit").write_text("new unit")
            (base / "backup/config.env").write_text("old config")
            (base / "backup/service").write_text("old unit")
            code = r'''
source "$1"
APP="$2/app"; CONFIG="$2/config"; UNIT="$2/unit"; BACKUP="$2/backup"
OLD_TARGET="$2/old"; LEGACY=""
SWITCHED=1; CONFIG_CHANGED=1; UNIT_CHANGED=1; OLD_ACTIVE=1; OLD_ENABLED=1
systemctl() { return 0; }
rollback
'''
            subprocess.run(["bash", "-c", code, "_", str(ROOT / "install.sh"), str(base)],
                           check=True, capture_output=True)
            self.assertEqual((base / "app").resolve(), base / "old")
            self.assertEqual((base / "config").read_text(), "old config")
            self.assertEqual((base / "unit").read_text(), "old unit")

    def test_rollback_restores_legacy_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "new").mkdir()
            (base / "legacy").mkdir()
            (base / "legacy/file").write_text("old code")
            (base / "app").symlink_to(base / "new")
            (base / "backup").mkdir()
            code = r'''
source "$1"
APP="$2/app"; CONFIG="$2/config"; UNIT="$2/unit"; BACKUP="$2/backup"
OLD_TARGET=""; LEGACY="$2/legacy"
SWITCHED=1; CONFIG_CHANGED=1; UNIT_CHANGED=1; OLD_ACTIVE=0; OLD_ENABLED=0
systemctl() { return 0; }
rollback
'''
            subprocess.run(["bash", "-c", code, "_", str(ROOT / "install.sh"), str(base)],
                           check=True, capture_output=True)
            self.assertFalse((base / "app").is_symlink())
            self.assertEqual((base / "app/file").read_text(), "old code")

class SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_smoke_uses_same_runner_and_requires_exact_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            config = settings_at(Path(temp))
            seen = {}
            class MockRunner:
                def __init__(self, settings):
                    seen["settings"] = settings
                async def run(self, prompt, cancel):
                    seen["prompt"] = prompt
                    return Result("success", text="AGY ready.")
            with patch("manage.os.geteuid", return_value=1000), patch("manage.Runner", MockRunner), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(await smoke(config), 0)
            self.assertEqual(seen["prompt"], SMOKE_PROMPT)
            command = build_command(seen["settings"], SMOKE_PROMPT)
            self.assertEqual(command[command.index("--output-format") + 1], "json")
            self.assertIn("--dangerously-skip-permissions", command)

    async def test_auth_quota_network_have_distinct_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            config = settings_at(Path(temp))
            for category, expected in [("auth", 10), ("quota", 12), ("network", 13),
                                       ("permission", 14), ("unknown", 15)]:
                class MockRunner:
                    def __init__(self, settings):
                        pass
                    async def run(self, prompt, cancel):
                        return Result("error", category=category)
                with self.subTest(category=category), patch("manage.os.geteuid", return_value=1000), \
                     patch("manage.Runner", MockRunner), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(await smoke(config), expected)

    async def test_nonempty_but_wrong_smoke_response_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            config = settings_at(Path(temp))
            class MockRunner:
                def __init__(self, settings):
                    pass
                async def run(self, prompt, cancel):
                    return Result("success", text="Some unrelated response")
            with patch("manage.os.geteuid", return_value=1000), patch("manage.Runner", MockRunner), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(await smoke(config), 0)

    async def test_smoke_refuses_root(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("manage.os.geteuid", return_value=0), self.assertRaises(ConfigError):
                await smoke(settings_at(Path(temp)))
