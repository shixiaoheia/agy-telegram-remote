import asyncio
import contextlib
import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
try:
    from .common import ROOT, config_values, settings_at
except ImportError:
    from common import ROOT, config_values, settings_at
from agy_runner import Result, SMOKE_PROMPT, build_command
from manage import (auth_login, classify_auth_error, kill_process_group, main,
                    oauth_environment, redact_auth_data, service_unit, smoke)
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
        unit = service_unit(Path("/root"), Path("/root"),
                            Path("/var/lib/agy-telegram-remote"))
        self.assertIn("User=root", unit)
        self.assertIn("Group=root", unit)
        self.assertIn("ProtectHome=no", unit)
        self.assertIn("/usr/bin/python3 -E -s -B", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("RuntimeDirectoryMode=0700", unit)
        self.assertNotIn("/venv/", unit)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", unit)

    def test_root_mode_service_unit(self):
        unit = service_unit(Path("/root"), Path("/root"),
                            Path("/var/lib/agy-telegram-remote"))
        self.assertIn("User=root", unit)
        self.assertIn("Group=root", unit)
        self.assertIn("ProtectHome=no", unit)

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
                    "--home", "/root"]
            inputs = iter([config_values()["TELEGRAM_BOT_TOKEN"], "12345"])
            with patch("sys.argv", argv), patch("builtins.input", side_effect=inputs) as inp, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            self.assertEqual(inp.call_count, 2)
            self.assertTrue(Settings.load(dest).skip_permissions)

    def test_prepare_config_preserves_old_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old, dest = base / "old", base / "candidate"
            work_dir = base / "project"
            work_dir.mkdir()
            old.write_text("\n".join(f"{k}={v}" for k, v in (config_values() | {
                "AGY_WORKSPACE": str(work_dir),
                "AGY_HOME": str(base),
                "AGY_SKIP_PERMISSIONS": "false", "AGY_TIMEOUT_SECONDS": "321",
            }).items()))
            dest.touch()
            argv = ["manage.py", "prepare-config", "--old", str(old),
                    "--output", str(dest), "--home", str(base)]
            with patch("sys.argv", argv), \
                 patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            value = Settings.load(dest)
            self.assertFalse(value.skip_permissions)
            self.assertEqual(value.timeout, 321)
            self.assertEqual(value.workspace, work_dir)

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

    def test_root_mode_defaults_in_installer(self):
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("APP_USER='root'", script)
        self.assertIn("APP_HOME=/root", script)
        self.assertNotIn("ensure_service_account", script)
        self.assertNotIn("userdel", script)

    def test_account_script_refuses_without_explicit_isolation(self):
        script = str(ROOT / "scripts/test_account_debian12.sh")
        base_env = {k: v for k, v in os.environ.items() if k != "AGY_TEST_ISOLATED_CONTAINER"}
        # Without flag and without env var
        r1 = subprocess.run(["bash", script], env=base_env, capture_output=True, text=True)
        self.assertEqual(r1.returncode, 2)
        self.assertIn("--confirm-isolated-environment", r1.stderr)

        # With flag but without env var
        r2 = subprocess.run(["bash", script, "--confirm-isolated-environment"],
                            env=base_env, capture_output=True, text=True)
        self.assertEqual(r2.returncode, 2)
        self.assertIn("AGY_TEST_ISOLATED_CONTAINER=1", r2.stderr)

        # With env var but without flag
        r3 = subprocess.run(["bash", script], env=base_env | {"AGY_TEST_ISOLATED_CONTAINER": "1"},
                            capture_output=True, text=True)
        self.assertEqual(r3.returncode, 2)
        self.assertIn("--confirm-isolated-environment", r3.stderr)

    def test_account_script_safety_and_scoped_cleanup(self):
        content = (ROOT / "scripts/test_account_debian12.sh").read_text(encoding="utf-8")
        self.assertIn("Test 1: Verify Debian 12 OS support check", content)
        self.assertIn("Test 2: Verify Root mode defaults", content)

    def test_deploy_lock_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "deploy.lock"
            code = r'''
stat() {
    if [[ "$1" == "-c" && "$2" == "%u" ]]; then
        echo "0"
        return 0
    fi
    command stat "$@"
}
source "$1"
DEPLOY_LOCK_FILE="$2"
exec 8>>"$DEPLOY_LOCK_FILE"
flock -n 8
acquire_deploy_lock
'''
            result = subprocess.run(["bash", "-c", code, "_", str(ROOT / "install.sh"), str(lock_path)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("已有安装、更新或卸载进程正在运行", result.stderr)

    def test_deploy_lock_rejects_non_root_dir_when_root(self):
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "deploy.lock"
            code = r'''
stat() {
    if [[ "$1" == "-c" && "$2" == "%u" ]]; then
        echo "1001"
        return 0
    fi
    command stat "$@"
}
source "$1"
DEPLOY_LOCK_FILE="$2"
acquire_deploy_lock
'''
            result = subprocess.run(["bash", "-c", code, "_", str(ROOT / "install.sh"), str(lock_path)],
                                    capture_output=True, text=True)
            if os.geteuid() == 0:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("锁定目录不受 root 管理", result.stderr)

    def test_help_does_not_acquire_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "deploy.lock"
            code = r'''
DEPLOY_LOCK_FILE="$2"
exec 8>>"$DEPLOY_LOCK_FILE"
flock -n 8
bash "$1" --help
'''
            result = subprocess.run(["bash", "-c", code, "_", str(ROOT / "install.sh"), str(lock_path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("用法：", result.stdout)

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

    async def test_smoke_allows_root_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            config = settings_at(Path(temp))
            class MockRunner:
                def __init__(self, settings):
                    pass
                async def run(self, prompt, cancel):
                    return Result("success", text="AGY ready.")
            with patch("manage.os.geteuid", return_value=0), patch("manage.Runner", MockRunner), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(await smoke(config), 0)


class AuthLoginTests(unittest.TestCase):
    def test_auth_login_when_already_authorized(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            mock_agy.write_text("#!/bin/bash\necho 'AGY ready.'\nexit 0\n")
            mock_agy.chmod(0o755)
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 0)
            self.assertIn("无需重新登录", buf.getvalue())

    def test_auth_login_interactive_flow_success(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "  https://accounts.google.com/o/oauth2/auth?test=1"
echo "Waiting for authentication (timeout 60s)..."
echo "Or, paste the authorization code here and press Enter:"
read -r code
if [[ "$code" == "good_code" ]]; then
  echo "AGY ready."
  exit 0
else
  echo 'Error: authentication failed: token exchange failed: oauth2: "invalid_grant"' >&2
  exit 1
fi
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf = io.StringIO()
            with patch("sys.stdout", buf), patch("builtins.input", return_value="good_code"):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Google 账号授权成功", out)
            self.assertIn("https://accounts.google.com/o/oauth2/auth?test=1", out)
            self.assertIn("正在等待授权", out)

    def test_auth_login_invalid_grant(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "  https://accounts.google.com/o/oauth2/auth?test=1"
echo "Waiting for authentication (timeout 60s)..."
echo "Or, paste the authorization code here and press Enter:"
read -r code
echo 'Error: authentication failed: token exchange failed: oauth2: "invalid_grant"' >&2
exit 1
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err), \
                 patch("builtins.input", return_value="bad_code"):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 1)
            self.assertIn("授权码无效或格式不正确", buf_err.getvalue())

    def test_auth_login_user_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "  https://accounts.google.com/o/oauth2/auth?test=1"
echo "Or, paste the authorization code here and press Enter:"
sleep 10
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf = io.StringIO()
            with patch("sys.stdout", buf), patch("builtins.input", side_effect=KeyboardInterrupt):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 20)
            self.assertIn("已取消授权流程", buf.getvalue())

    def test_auth_login_missing_executable(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "nonexistent"
            buf_err = io.StringIO()
            with patch("sys.stderr", buf_err):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 1)
            self.assertIn("执行文件不存在", buf_err.getvalue())

    def test_auth_login_cli_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            mock_agy = base / "mock_agy"
            mock_agy.write_text("#!/bin/bash\necho 'AGY ready.'\nexit 0\n")
            mock_agy.chmod(0o755)

            # Test dispatch with --config
            dest = base / "candidate"
            dest.write_text("\n".join(f"{k}={v}" for k, v in (config_values() | {
                "AGY_PATH": str(mock_agy),
                "AGY_HOME": str(base),
                "AGY_WORKSPACE": str(base),
                "STATE_DIR": "/var/lib/agy-telegram-remote",
            }).items()))
            argv_config = ["manage.py", "auth-login", "--config", str(dest)]
            with patch("sys.argv", argv_config), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)

            # Test dispatch with explicit flags
            argv_flags = ["manage.py", "auth-login", "--agy", str(mock_agy),
                          "--home", str(base), "--workspace", str(base)]
            with patch("sys.argv", argv_flags), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)

    def test_auth_login_child_closed_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
exit 1
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            import time
            def fake_input(prompt):
                time.sleep(0.05)
                return "some_code"
            with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err), \
                 patch("builtins.input", side_effect=fake_input):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 1)
            self.assertIn("授权会话已结束，请重新开始", buf_err.getvalue())
            self.assertNotIn("正在验证授权码", buf_out.getvalue())

    def test_auth_login_write_oserror_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Or, paste the authorization code here and press Enter:"
sleep 10
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err), \
                 patch("builtins.input", return_value="some_code"), \
                 patch("os.write", side_effect=OSError(5, "Input/output error")):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 1)
            self.assertIn("授权会话已结束，请重新开始", buf_err.getvalue())
            self.assertNotIn("正在验证授权码", buf_out.getvalue())

    def test_auth_login_slow_token_exchange(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
read -r code
sleep 1
if [[ "$code" == "valid_code" ]]; then
  echo "AGY ready."
  exit 0
fi
exit 1
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf = io.StringIO()
            with patch("sys.stdout", buf), patch("builtins.input", return_value="valid_code"):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5, exchange_timeout=10)
            self.assertEqual(rc, 0)
            self.assertIn("Google 账号授权成功", buf.getvalue())

    def test_auth_login_unknown_error_redacted_and_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            secret_code = "4/0Abc1234567890XYZ_SuperSecretAuthCode"
            secret_token = "ya29.a0AfH6SMD_TokenXYZSecret123"
            script = f'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
read -r code
echo "Internal CLI dump: code={secret_code} token={secret_token} unexpected_err_code_xyz" >&2
exit 42
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err), \
                 patch("builtins.input", return_value=secret_code):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 1)
            err = buf_err.getvalue()
            self.assertNotIn(secret_code, err)
            self.assertNotIn(secret_token, err)
            self.assertIn("[REDACTED_CODE]", err)
            self.assertIn("[REDACTED_TOKEN]", err)
            self.assertIn("unexpected_err_code_xyz", err)

    def test_oauth_environment_preserves_ssh_and_proxy_but_strips_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            inherited = {
                "HOME": "/root",
                "PATH": "/bin:/usr/bin",
                "SSH_CLIENT": "1.2.3.4 5678 22",
                "SSH_CONNECTION": "1.2.3.4 5678 10.0.0.1 22",
                "SSH_TTY": "/dev/pts/1",
                "HTTPS_PROXY": "http://127.0.0.1:7890",
                "HTTP_PROXY": "http://127.0.0.1:7890",
                "ALL_PROXY": "socks5://127.0.0.1:1080",
                "NO_PROXY": "localhost,127.0.0.1",
                "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567",
                "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567",
                "SSH_AUTH_SOCK": "/tmp/ssh-secret/agent.123",
                "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
                "OPENAI_API_KEY": "sk-secret123456",
            }
            env = oauth_environment(Path(td), inherited)
            self.assertEqual(env["HOME"], td)
            self.assertEqual(env["SSH_CLIENT"], "1.2.3.4 5678 22")
            self.assertEqual(env["SSH_CONNECTION"], "1.2.3.4 5678 10.0.0.1 22")
            self.assertEqual(env["SSH_TTY"], "/dev/pts/1")
            self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
            self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7890")
            self.assertEqual(env["ALL_PROXY"], "socks5://127.0.0.1:1080")
            self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
            self.assertNotIn("BOT_TOKEN", env)
            self.assertNotIn("SSH_AUTH_SOCK", env)
            self.assertNotIn("AWS_ACCESS_KEY_ID", env)
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_auth_login_timeout_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
read -r code
sleep 30
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_err = io.StringIO()
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", buf_err), \
                 patch("builtins.input", return_value="slow_code"):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5, exchange_timeout=0.3)
            self.assertEqual(rc, 1)
            self.assertIn("超时", buf_err.getvalue())

    def test_empty_line_and_residual_newline_not_submitted_prematurely(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
read -r code
if [[ -z "$code" ]]; then
  echo "RECEIVED_EMPTY" >&2
  exit 2
fi
if [[ "$code" == "valid_code" ]]; then
  echo "AGY ready."
  exit 0
fi
exit 1
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            inputs = ["", "   ", "valid_code"]
            with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err), \
                 patch("builtins.input", side_effect=inputs):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 0)
            out = buf_out.getvalue()
            self.assertIn("输入为空", out)
            self.assertIn("正在验证授权码", out)
            self.assertNotIn("RECEIVED_EMPTY", buf_err.getvalue())

    def test_url_input_extracts_code(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
read -r code
if [[ "$code" == "extracted_code_123" ]]; then
  echo "AGY ready."
  exit 0
fi
echo "RECEIVED: $code" >&2
exit 1
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_out = io.StringIO()
            with patch("sys.stdout", buf_out), \
                 patch("builtins.input", return_value="http://localhost:8080/oauth/callback?state=xyz&code=extracted_code_123"):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 0)

    def test_cli_exits_before_user_input_promptly_detected(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
exit 7
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_err = io.StringIO()
            buf_out = io.StringIO()
            with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 1)
            err = buf_err.getvalue()
            self.assertIn("授权会话已结束", err)
            self.assertIn("请勿在 Shell 提示符后继续粘贴授权码", err)

    def test_user_input_timeout_vs_network_exchange_timeout_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
read -r code
sleep 10
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)

            # 1. User input timeout
            buf_err1 = io.StringIO()
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", buf_err1), \
                 patch("manage._read_code_line", return_value=(None, "timeout")):
                rc1 = auth_login(mock_agy, Path(td), Path(td), timeout=0.2)
            self.assertEqual(rc1, 1)
            self.assertIn("等待用户输入授权码超时", buf_err1.getvalue())

            # 2. Exchange timeout
            buf_err2 = io.StringIO()
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", buf_err2), \
                 patch("builtins.input", return_value="my_code"):
                rc2 = auth_login(mock_agy, Path(td), Path(td), timeout=5, exchange_timeout=0.2)
            self.assertEqual(rc2, 1)
            self.assertIn("通信换票超时", buf_err2.getvalue())

    def test_prompt_split_in_chunks_without_trailing_newline_recognized(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
printf 'Authentication required. Please visit the URL to log in:\n'
printf 'https://accounts.google.com/o/oauth2/auth?test=split\n'
printf 'Or, paste the authorization code '
sleep 0.1
printf 'here and press Enter:'
read -r code
if [[ "$code" == "chunk_ok" ]]; then
  echo "AGY ready."
  exit 0
fi
exit 1
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_out = io.StringIO()
            with patch("sys.stdout", buf_out), patch("builtins.input", return_value="chunk_ok"):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 0)
            self.assertIn("https://accounts.google.com/o/oauth2/auth?test=split", buf_out.getvalue())
            self.assertIn("Google 账号授权成功", buf_out.getvalue())

    def test_cli_fast_exit_trailing_output_not_lost(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            script = r'''#!/bin/bash
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
read -r code
echo "FATAL_CLI_DIAGNOSTIC_TRAILING_ERROR_XYZ" >&2
exit 33
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            buf_err = io.StringIO()
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", buf_err), \
                 patch("builtins.input", return_value="any_code"):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 1)
            self.assertIn("FATAL_CLI_DIAGNOSTIC_TRAILING_ERROR_XYZ", buf_err.getvalue())

    def test_auth_error_categories_distinct(self):
        from manage import classify_auth_error
        self.assertIn("授权码无效或格式不正确", classify_auth_error("oauth2: invalid_grant code expired", 1, submitted=True))
        self.assertIn("配额受限", classify_auth_error("resource_exhausted: quota exceeded 429", 1, submitted=True))
        self.assertIn("网络连接失败", classify_auth_error("dial tcp: connection refused", 1, submitted=True))
        self.assertIn("协议或参数异常", classify_auth_error("unknown flag --foo panic:", 1, submitted=True))
        self.assertIn("超时", classify_auth_error("context deadline exceeded", 1, submitted=True))
        self.assertIn("地区或资格受限", classify_auth_error("location not eligible unsupported country", 1, submitted=True))

    def test_child_ignoring_sigterm_escalates_to_sigkill_no_nameerror(self):
        with tempfile.TemporaryDirectory() as td:
            mock_script = Path(td) / "stubborn.sh"
            mock_script.write_text("#!/bin/bash\ntrap '' SIGTERM\nwhile true; do sleep 1; done\n")
            mock_script.chmod(0o755)
            proc = subprocess.Popen([str(mock_script)], start_new_session=True)
            try:
                self.assertIsNone(proc.poll())
                kill_process_group(proc, timeout=0.1, kill_timeout=0.2)
                self.assertIsNotNone(proc.poll())
            finally:
                try:
                    proc.kill()
                except OSError:
                    pass

    def test_child_descendants_cleaned_no_pty_leak(self):
        with tempfile.TemporaryDirectory() as td:
            mock_agy = Path(td) / "mock_agy"
            pid_file = Path(td) / "child.pid"
            script = f'''#!/bin/bash
sleep 60 &
echo $! > "{pid_file}"
echo "Authentication required. Please visit the URL to log in:"
echo "Or, paste the authorization code here and press Enter:"
wait $!
'''
            mock_agy.write_text(script)
            mock_agy.chmod(0o755)
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()), \
                 patch("builtins.input", side_effect=KeyboardInterrupt):
                rc = auth_login(mock_agy, Path(td), Path(td), timeout=5)
            self.assertEqual(rc, 20)
            if pid_file.exists() and pid_file.read_text().strip():
                child_pid = int(pid_file.read_text().strip())
                time.sleep(0.2)
                try:
                    os.kill(child_pid, 0)
                    self.fail("Descendant child process was not killed")
                except ProcessLookupError:
                    pass

    def test_quota_network_smoke_failure_preserves_credentials_no_reauth(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            token_file = base / "antigravity-oauth-token"
            token_file.write_text("my_existing_valid_token")
            bash_snippet = f'''
APP_HOME="{base}"
REAUTH=0
installed_now=0
smoke_rc=12
TOKEN_ORIGINAL="{token_file}"
TOKEN_BACKUP="{token_file}.bak.$$"

if [[ "$smoke_rc" -eq 10 || "$installed_now" == 1 || "$REAUTH" == 1 ]]; then
  if [[ -f "$TOKEN_ORIGINAL" ]]; then
    mv -f -- "$TOKEN_ORIGINAL" "$TOKEN_BACKUP"
  fi
  auth_called=1
elif [[ "$smoke_rc" -ne 0 ]]; then
  auth_called=0
fi

if [[ "$auth_called" == 0 && -f "$TOKEN_ORIGINAL" ]]; then
  echo "PRESERVED"
fi
'''
            proc = subprocess.run(["/bin/bash", "-c", bash_snippet], capture_output=True, text=True)
            self.assertIn("PRESERVED", proc.stdout)
            self.assertTrue(token_file.exists())
            self.assertEqual(token_file.read_text(), "my_existing_valid_token")

    def test_auth_cancel_or_sigterm_restores_token_backup(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            token_file = base / "antigravity-oauth-token"
            token_file.write_text("precious_credentials")
            backup_file = base / "antigravity-oauth-token.bak.1234"
            token_file.rename(backup_file)
            self.assertFalse(token_file.exists())
            self.assertTrue(backup_file.exists())

            bash_snippet = f'''
TOKEN_ORIGINAL="{token_file}"
TOKEN_BACKUP="{backup_file}"
if [[ -n "$TOKEN_BACKUP" && -f "$TOKEN_BACKUP" && -n "$TOKEN_ORIGINAL" ]]; then
  mv -f -- "$TOKEN_BACKUP" "$TOKEN_ORIGINAL" 2>/dev/null || true
  TOKEN_BACKUP=
fi
'''
            subprocess.run(["/bin/bash", "-c", bash_snippet], check=True)
            self.assertTrue(token_file.exists())
            self.assertEqual(token_file.read_text(), "precious_credentials")
            self.assertFalse(backup_file.exists())

    def test_full_environment_chain_preserves_ssh_and_proxy_strips_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            script = f'''
APP_HOME="{base}"
SSH_CLIENT="1.2.3.4 5678 22"
SSH_CONNECTION="1.2.3.4 5678 10.0.0.1 22"
SSH_TTY="/dev/pts/1"
HTTPS_PROXY="http://127.0.0.1:7890"
HTTP_PROXY="http://127.0.0.1:7890"
TELEGRAM_BOT_TOKEN="secret_bot_token"
BOT_TOKEN="secret_bot_token"
SSH_AUTH_SOCK="/tmp/secret.sock"
AWS_SECRET_ACCESS_KEY="secret_aws"

as_user() {{
  env -i \\
    HOME="$APP_HOME" USER="root" LOGNAME="root" \\
    PATH="$APP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \\
    TERM="${{TERM:-xterm-256color}}" \\
    TMPDIR="${{TMPDIR:-/tmp}}" \\
    SSH_CLIENT="${{SSH_CLIENT-}}" SSH_CONNECTION="${{SSH_CONNECTION-}}" SSH_TTY="${{SSH_TTY-}}" \\
    HTTP_PROXY="${{HTTP_PROXY-}}" HTTPS_PROXY="${{HTTPS_PROXY-}}" ALL_PROXY="${{ALL_PROXY-}}" NO_PROXY="${{NO_PROXY-}}" \\
    "$@"
}}

as_user python3 -c "
import os, sys
sys.path.insert(0, '{ROOT}')
from manage import oauth_environment
from pathlib import Path
env = oauth_environment(Path('{base}'), os.environ)
assert env['SSH_CLIENT'] == '1.2.3.4 5678 22'
assert env['SSH_CONNECTION'] == '1.2.3.4 5678 10.0.0.1 22'
assert env['SSH_TTY'] == '/dev/pts/1'
assert env['HTTPS_PROXY'] == 'http://127.0.0.1:7890'
assert 'TELEGRAM_BOT_TOKEN' not in env
assert 'BOT_TOKEN' not in env
assert 'SSH_AUTH_SOCK' not in env
assert 'AWS_SECRET_ACCESS_KEY' not in env
print('ENV_CHAIN_SUCCESS')
"
'''
            res = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("ENV_CHAIN_SUCCESS", res.stdout)

    def test_tokens_auth_codes_and_refresh_tokens_redacted(self):
        from manage import redact_auth_data
        sample = (
            "Login failed for code 4/0Abc1234567890XYZ_SuperSecret and token ya29.a0AfH6SMD_TokenXYZSecret123. "
            "Redirect uri was http://localhost:8080/?code=4/0Abc1234567890XYZ_SuperSecret&token=ya29.xyz "
            "and refresh_token=1//0987654321xyz. Bot token is 123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567. "
            "Proxy was http://user:supersecretpass@proxy.example.com:8080"
        )
        redacted = redact_auth_data(sample, code="4/0Abc1234567890XYZ_SuperSecret")
        self.assertNotIn("4/0Abc1234567890XYZ_SuperSecret", redacted)
        self.assertNotIn("ya29.a0AfH6SMD_TokenXYZSecret123", redacted)
        self.assertNotIn("123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567", redacted)
        self.assertNotIn("supersecretpass", redacted)
        self.assertIn("[REDACTED_CODE]", redacted)
        self.assertIn("[REDACTED_TOKEN]", redacted)
        self.assertIn("[REDACTED_BOT_TOKEN]", redacted)
        self.assertIn("[REDACTED_PASSWORD]", redacted)
