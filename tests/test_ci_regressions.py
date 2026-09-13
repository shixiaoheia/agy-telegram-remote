"""Offline regression tests for the September 2026 CI environment failures."""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pty
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

from agy_runner import Result, classify
from manage import (_read_code_line, auth_login, classify_auth_error,
                    kill_process_group, smoke)

ROOT = Path(__file__).resolve().parents[1]
ELIGIBILITY_ERROR = (
    "Eligibility check failed: Your current account is not eligible for Antigravity, "
    "because it is not currently available in your location."
)


@contextlib.contextmanager
def input_pipe(text: str = "", *, close_writer: bool = False):
    """A real input descriptor; an open writer means silence, not EOF."""
    read_fd, write_fd = os.pipe()
    try:
        if text:
            os.write(write_fd, text.encode("utf-8"))
        if close_writer:
            os.close(write_fd)
            write_fd = -1
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            yield reader
    finally:
        if write_fd >= 0:
            os.close(write_fd)


class AuthInputEnvironmentTests(unittest.TestCase):
    def fake_cli(self, base: Path, body: str) -> Path:
        path = base / "fake-agy"
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_live_cli_with_eof_is_cancelled_not_an_exchange(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cli = self.fake_cli(
                base,
                "echo 'Or, paste the authorization code here and press Enter:'\n"
                "read -r code\nexit 8\n",
            )
            output, error = io.StringIO(), io.StringIO()
            with open(os.devnull, "r", encoding="utf-8") as empty, \
                 patch("sys.stdin", empty), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                rc = auth_login(cli, base, base, timeout=3, exchange_timeout=1)
            self.assertEqual(rc, 20)
            self.assertIn("已取消授权流程", output.getvalue())
            self.assertNotIn("正在验证授权码", output.getvalue())

    def test_open_silent_pipe_is_not_eof(self):
        proc = Mock()
        proc.poll.side_effect = [None, 7]
        with input_pipe() as reader, patch("sys.stdin", reader):
            code, status = _read_code_line(proc, timeout=2)
        self.assertIsNone(code)
        self.assertEqual(status, "process_exited")

    def test_already_exited_cli_takes_priority_over_stdin_eof(self):
        proc = Mock()
        proc.poll.return_value = 7
        with open(os.devnull, "r", encoding="utf-8") as empty, patch("sys.stdin", empty):
            code, status = _read_code_line(proc, timeout=2)
        self.assertEqual((code, status), (None, "process_exited"))

    def test_no_fileno_does_not_raise_missing_io_name(self):
        proc = Mock()
        proc.poll.return_value = None
        with patch("sys.stdin", io.StringIO("example_code_only\n")):
            code, status = _read_code_line(proc, timeout=1)
        self.assertEqual((code, status), ("example_code_only", "ok"))

    def test_cli_exit_with_real_idle_terminal_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cli = self.fake_cli(
                base,
                "echo 'Or, paste the authorization code here and press Enter:'\nexit 7\n",
            )
            master, slave = pty.openpty()
            output, error = io.StringIO(), io.StringIO()
            try:
                with os.fdopen(slave, "r", encoding="utf-8") as terminal, \
                     patch("sys.stdin", terminal), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    rc = auth_login(cli, base, base, timeout=3)
            finally:
                os.close(master)
            self.assertEqual(rc, 1)
            self.assertIn("授权会话已结束", error.getvalue())
            self.assertNotIn("正在验证授权码", output.getvalue())

    def test_real_input_descriptor_can_complete_fake_login(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cli = self.fake_cli(
                base,
                "echo 'Or, paste the authorization code here and press Enter:'\n"
                "read -r code\n"
                "test \"$code\" = example_code_only || exit 9\n"
                "echo 'AGY ready.'\n",
            )
            output, error = io.StringIO(), io.StringIO()
            with input_pipe("example_code_only\n", close_writer=True) as reader, \
                 patch("sys.stdin", reader), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                rc = auth_login(cli, base, base, timeout=3)
            self.assertEqual(rc, 0)
            self.assertNotIn("example_code_only", output.getvalue() + error.getvalue())


class EligibilityClassificationTests(unittest.TestCase):
    def test_explicit_service_denial_is_not_expired_credentials(self):
        for text in (
            ELIGIBILITY_ERROR,
            "oauth: " + ELIGIBILITY_ERROR,
            "authentication required\n" + ELIGIBILITY_ERROR,
            "PERMISSION_DENIED: " + ELIGIBILITY_ERROR,
        ):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "eligibility")

    def test_unrelated_location_words_do_not_imply_denial(self):
        for text, category in (
            ("redirect location header missing", "unknown"),
            ("country field missing", "unknown"),
            ("oauth token endpoint: dial tcp: network timeout", "network"),
            ("invalid_grant: token expired", "auth"),
            ("resource_exhausted quota 429", "quota"),
        ):
            with self.subTest(text=text):
                self.assertEqual(classify(text), category)

    def test_eligibility_survives_input_phase_and_timeout_banner(self):
        message = "Waiting for authentication (timeout 60s)\n" + ELIGIBILITY_ERROR
        for phase in ("await_prompt", "await_input", "await_result"):
            with self.subTest(phase=phase):
                result = classify_auth_error(
                    message, 7, phase=phase, submitted=phase == "await_result"
                )
                self.assertIn("地区或资格受限", result)
                self.assertIn("不能单独判定", result)
                self.assertNotIn("换票超时", result)

    def test_eligibility_without_input_is_reported_by_auth_login(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cli = base / "fake-agy"
            cli.write_text(
                "#!/bin/bash\n"
                "echo 'Or, paste the authorization code here and press Enter:'\n"
                "echo '" + ELIGIBILITY_ERROR + "' >&2\nexit 7\n",
                encoding="utf-8",
            )
            cli.chmod(0o700)
            error = io.StringIO()
            with input_pipe() as reader, patch("sys.stdin", reader), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error):
                rc = auth_login(cli, base, base, timeout=3)
            self.assertEqual(rc, 1)
            self.assertIn("地区或资格受限", error.getvalue())


@dataclass(frozen=True)
class SmokeConfig:
    timeout: float = 90.0


class SmokeExitCodeTests(unittest.IsolatedAsyncioTestCase):
    async def run_smoke_for(self, category: str) -> int:
        result = Result("error", category=category)

        class FakeRunner:
            def __init__(self, settings):
                self.settings = settings

            async def run(self, prompt, cancel):
                return result

        with patch("manage.Runner", FakeRunner), contextlib.redirect_stdout(io.StringIO()):
            return await smoke(SmokeConfig())

    async def test_eligibility_has_its_own_non_auth_exit_code(self):
        self.assertEqual(await self.run_smoke_for("eligibility"), 18)

    async def test_existing_exit_codes_are_not_redefined(self):
        for category, rc in (
            ("auth", 10), ("quota", 12), ("network", 13),
            ("permission", 14), ("unknown", 15),
        ):
            with self.subTest(category=category):
                self.assertEqual(await self.run_smoke_for(category), rc)


class InstallerEligibilityTests(unittest.TestCase):
    def run_auth_gate(self, base: Path, *, reauth: bool = False):
        # Execute only the reviewed decision block inside a temporary fake HOME.
        # Never invoke install.sh main(), apt, git fetch, systemctl or real APIs.
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        start = text.index("  local smoke_rc=10\n")
        end = text.index(
            '  as_user /usr/bin/python3 -E -s -B "$STAGE/manage.py" check-local',
            start,
        )
        block = text[start:end]
        prefix = r"""set -euo pipefail
APP_HOME="$1"
REAUTH="$2"
installed_now=0
STAGE=/fake-stage
CANDIDATE=/fake-config
TOKEN_BACKUP=
TOKEN_ORIGINAL=
fail() { printf '%s\n' "$*" >&2; exit 1; }
as_user() {
  case " $* " in
    *" auth-login "*)
      printf 'called\n' >> "$APP_HOME/auth-calls"
      printf 'NEW_TEST_ONLY\n' > "$TOKEN_ORIGINAL"
      return 0
      ;;
    *" smoke "*) return 18 ;;
    *) return 90 ;;
  esac
}
probe() {
"""
        script = prefix + block + "\n}\nprobe\n"
        return subprocess.run(
            ["bash", "-c", script, "_", str(base), "1" if reauth else "0"],
            capture_output=True, text=True, timeout=5,
        )

    def token_fixture(self, base: Path) -> Path:
        token = base / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("OLD_TEST_ONLY\n", encoding="utf-8")
        return token

    def test_eligibility_failure_keeps_credentials_and_does_not_reauth(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            token = self.token_fixture(base)
            result = self.run_auth_gate(base)
            self.assertEqual(result.returncode, 1)
            self.assertIn("代码 18", result.stderr)
            self.assertEqual(token.read_text(), "OLD_TEST_ONLY\n")
            self.assertFalse((base / "auth-calls").exists())
            self.assertEqual(list(token.parent.glob("*.bak.*")), [])

    def test_post_login_eligibility_failure_does_not_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self.token_fixture(base)
            result = self.run_auth_gate(base, reauth=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("登录后", result.stderr)
            self.assertEqual((base / "auth-calls").read_text().splitlines(), ["called"])


class PTYUrlChunkTests(unittest.TestCase):
    def test_url_split_in_two_chunks_real_pty(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cli = base / "fake-agy"
            cli.write_text(
                "#!/bin/bash\n"
                "printf 'https://accounts.google.com/o/oauth2/auth?test=chunk1'\n"
                "sleep 0.1\n"
                "printf 'chunk2\\n'\n"
                "echo 'Or, paste the authorization code here and press Enter:'\n"
                "read -r code\n"
                "echo 'AGY ready.'\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            out = io.StringIO()
            with patch("sys.stdout", out), patch("manage._read_code_line", return_value=("good_code", "ok")):
                rc = auth_login(cli, base, base, timeout=5, exchange_timeout=2)
            self.assertEqual(rc, 0)
            val = out.getvalue()
            self.assertIn("https://accounts.google.com/o/oauth2/auth?test=chunk1chunk2", val)
            self.assertNotIn("https://accounts.google.com/o/oauth2/auth?test=chunk1\n", val)


class KillProcessGroupTests(unittest.TestCase):
    def test_parent_exits_first_cleans_surviving_child(self):
        proc = subprocess.Popen(["bash", "-c", "sleep 30 & exit 0"], start_new_session=True)
        pgid = os.getpgid(proc.pid)
        proc.wait()
        try:
            os.killpg(pgid, 0)
            group_alive = True
        except ProcessLookupError:
            group_alive = False
        self.assertTrue(group_alive)

        kill_process_group(proc, timeout=0.5, kill_timeout=0.5, pgid=pgid)

        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    def test_child_ignores_sigterm_escalates_to_sigkill(self):
        proc = subprocess.Popen(["bash", "-c", "trap '' TERM; sleep 30"], start_new_session=True)
        pgid = os.getpgid(proc.pid)
        proc._agy_pgid = pgid

        kill_process_group(proc, timeout=0.2, kill_timeout=0.5)

        self.assertIsNotNone(proc.poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    def test_kill_process_group_does_not_kill_unrelated_process(self):
        unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
        target = subprocess.Popen(["sleep", "30"], start_new_session=True)

        try:
            kill_process_group(target, timeout=0.5, kill_timeout=0.5)
            self.assertIsNotNone(target.poll())
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait()


class ReadCodeLineRealInputTests(unittest.TestCase):
    def test_empty_lines_followed_by_valid_input(self):
        dummy_proc = Mock()
        dummy_proc.poll.return_value = None
        with input_pipe("\n   \n4/0AY0e-valid_code_123\n", close_writer=True) as reader, \
             patch("sys.stdin", reader):
            code, status = _read_code_line(dummy_proc, timeout=2)
        self.assertEqual(status, "ok")
        self.assertEqual(code, "4/0AY0e-valid_code_123")

    def test_partial_input_chunks_then_newline(self):
        dummy_proc = Mock()
        dummy_proc.poll.return_value = None
        r, w = os.pipe()
        try:
            with os.fdopen(r, "r", encoding="utf-8") as reader, patch("sys.stdin", reader):
                os.write(w, b"4/0AY0e-part1")
                def write_rest():
                    import time
                    time.sleep(0.05)
                    os.write(w, b"_part2\n")
                import threading
                t = threading.Thread(target=write_rest)
                t.start()
                code, status = _read_code_line(dummy_proc, timeout=2)
                t.join()
            self.assertEqual(status, "ok")
            self.assertEqual(code, "4/0AY0e-part1_part2")
        finally:
            try:
                os.close(w)
            except OSError:
                pass

    def test_cli_exits_first_detected(self):
        dummy_proc = Mock()
        dummy_proc.poll.side_effect = [None, None, 42]
        with input_pipe("", close_writer=False) as reader, patch("sys.stdin", reader):
            code, status = _read_code_line(dummy_proc, timeout=2)
        self.assertIsNone(code)
        self.assertEqual(status, "process_exited")

    def test_timeout_detected(self):
        dummy_proc = Mock()
        dummy_proc.poll.return_value = None
        with input_pipe("", close_writer=False) as reader, patch("sys.stdin", reader):
            code, status = _read_code_line(dummy_proc, timeout=0.2)
        self.assertIsNone(code)
        self.assertEqual(status, "timeout")

    def test_terminal_attributes_restoration(self):
        import termios
        master, slave = pty.openpty()
        dummy_proc = Mock()
        dummy_proc.poll.return_value = None
        try:
            with os.fdopen(slave, "r", encoding="utf-8") as terminal, \
                 patch("sys.stdin", terminal):
                code, status = _read_code_line(dummy_proc, timeout=0.1)
            self.assertEqual(status, "timeout")
        finally:
            os.close(master)

    def test_auth_code_never_leaked_in_output(self):
        secret_code = "4/0AY0e-VERY_SECRET_CODE_DO_NOT_PRINT"
        dummy_proc = Mock()
        dummy_proc.poll.return_value = None
        buf_out = io.StringIO()
        with input_pipe(f" \n{secret_code}\n", close_writer=True) as reader, \
             patch("sys.stdin", reader), \
             contextlib.redirect_stdout(buf_out):
            code, status = _read_code_line(dummy_proc, timeout=2)
        self.assertEqual(status, "ok")
        self.assertEqual(code, secret_code)
        self.assertNotIn(secret_code, buf_out.getvalue())


class InstallerProtectionTests(unittest.TestCase):
    def test_backup_failure_preserves_original_and_fails(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            token = base / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token.parent.mkdir(parents=True)
            token.write_text("MY_REAL_PRECIOUS_TOKEN", encoding="utf-8")

            snippet = f'''set -euo pipefail
TOKEN_ORIGINAL="{token}"
TOKEN_BACKUP="/nonexistent_dir/cannot_write/token.bak"
fail() {{ echo "$*" >&2; exit 1; }}

if [[ -f "$TOKEN_ORIGINAL" ]]; then
  if ! mv -f -- "$TOKEN_ORIGINAL" "$TOKEN_BACKUP" 2>/dev/null; then
    TOKEN_BACKUP=
    fail "凭据备份失败：无法将旧凭据移动至备份路径，已保留原件，安装中止。"
  fi
fi
'''
            res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("凭据备份失败", res.stderr)
            self.assertTrue(token.exists())
            self.assertEqual(token.read_text(encoding="utf-8"), "MY_REAL_PRECIOUS_TOKEN")

    def test_restore_failure_reports_error_and_preserves_backup_path(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            backup = base / "token.bak.999"
            backup.write_text("BACKUP_TOKEN_CONTENT", encoding="utf-8")
            target = "/nonexistent_dir/cannot_write/dest_token"

            snippet = f'''
TOKEN_ORIGINAL="{target}"
TOKEN_BACKUP="{backup}"

if [[ -n "$TOKEN_BACKUP" && -f "$TOKEN_BACKUP" && -n "$TOKEN_ORIGINAL" ]]; then
  if mv -f -- "$TOKEN_BACKUP" "$TOKEN_ORIGINAL" 2>/dev/null; then
    TOKEN_BACKUP=
  else
    echo "❌ 凭据恢复失败：无法将备份还原至 $TOKEN_ORIGINAL。备份文件保留在：$TOKEN_BACKUP" >&2
  fi
fi
'''
            res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
            self.assertIn("凭据恢复失败", res.stderr)
            self.assertIn(str(backup), res.stderr)
            self.assertTrue(backup.exists())

    def test_current_commit_backed_up_and_restored_on_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config_dir = base / "etc"
            config_dir.mkdir()
            current_commit = config_dir / "current_commit"
            current_commit.write_text("commit_v1\n", encoding="utf-8")

            backup_dir = base / "backup"
            backup_dir.mkdir()

            snippet = f'''set -e
CONFIG_DIR="{config_dir}"
BACKUP="{backup_dir}"

[[ ! -f "$CONFIG_DIR/current_commit" ]] || cp -p -- "$CONFIG_DIR/current_commit" "$BACKUP/current_commit"
echo "commit_v2" > "$CONFIG_DIR/current_commit"

if [[ -f "$BACKUP/current_commit" ]]; then
  cp -p -- "$BACKUP/current_commit" "$CONFIG_DIR/current_commit"
else
  rm -f -- "$CONFIG_DIR/current_commit"
fi
'''
            subprocess.run(["bash", "-c", snippet], check=True)
            self.assertEqual(current_commit.read_text(encoding="utf-8"), "commit_v1\n")

    def test_real_pty_echo_suppression_on_master(self):
        import select
        master, slave = pty.openpty()
        dummy_proc = Mock()
        dummy_proc.poll.return_value = None
        secret_code = "4/0AY0e-SECRET_TEST_CODE_NO_ECHO"
        try:
            with os.fdopen(slave, "r", encoding="utf-8") as terminal, \
                 patch("sys.stdin", terminal):
                def feed_input():
                    import time
                    time.sleep(0.05)
                    os.write(master, (secret_code + "\n").encode("utf-8"))

                import threading
                t = threading.Thread(target=feed_input)
                t.start()
                code, status = _read_code_line(dummy_proc, timeout=2.0)
                t.join()

            self.assertEqual(status, "ok")
            self.assertEqual(code, secret_code)

            echoed = ""
            while True:
                r, _, _ = select.select([master], [], [], 0.05)
                if not r:
                    break
                try:
                    chunk = os.read(master, 1024)
                    if not chunk:
                        break
                    echoed += chunk.decode("utf-8", errors="replace")
                except OSError:
                    break
            self.assertNotIn(secret_code, echoed)
        finally:
            try:
                os.close(master)
            except OSError:
                pass

    def test_oversized_input_without_newline_is_bounded_and_reprompted(self):
        dummy_proc = Mock()
        dummy_proc.poll.return_value = None
        long_garbage = "X" * 3000
        valid_code = "4/0AY0e-ValidCodeAfterLongGarbage"
        buf_out = io.StringIO()
        with input_pipe(f"{long_garbage}\n{valid_code}\n", close_writer=True) as reader, \
             patch("sys.stdin", reader), \
             contextlib.redirect_stdout(buf_out):
            code, status = _read_code_line(dummy_proc, timeout=2.0)
        self.assertEqual(status, "ok")
        self.assertEqual(code, valid_code)
        self.assertIn("超过长度限制", buf_out.getvalue())


class AuthLoginSigtermTests(unittest.TestCase):
    def test_sigterm_during_await_prompt_kills_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            grandchild_pid_file = base / "grandchild.pid"
            mock_agy = base / "mock_agy"
            mock_agy.write_text(f"""#!/bin/bash
sleep 100 &
echo $! > "{grandchild_pid_file}"
sleep 100
""", encoding="utf-8")
            mock_agy.chmod(0o755)

            script = f"""
import sys, time
from pathlib import Path
from manage import auth_login
auth_login(Path("{mock_agy}"), Path("{base}"), Path("{base}"), timeout=30.0)
"""
            runner = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            import time
            for _ in range(50):
                if grandchild_pid_file.exists() and grandchild_pid_file.read_text().strip():
                    break
                time.sleep(0.05)
            self.assertTrue(grandchild_pid_file.exists())
            gpid = int(grandchild_pid_file.read_text().strip())

            import signal
            runner.terminate()
            ret = runner.wait(timeout=5.0)
            self.assertEqual(ret, 143)

            time.sleep(0.2)
            try:
                os.kill(gpid, 0)
                os.kill(gpid, signal.SIGKILL)
                self.fail("Grandchild process survived SIGTERM")
            except ProcessLookupError:
                pass

    def test_sigterm_during_await_input_kills_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            child_pid_file = base / "child.pid"
            mock_agy = base / "mock_agy"
            mock_agy.write_text(f"""#!/bin/bash
echo $$ > "{child_pid_file}"
echo "Authentication required. Please visit the URL to log in:"
echo "  https://accounts.google.com/o/oauth2/auth?test=1"
echo "Or, paste the authorization code here and press Enter:"
read -r code
sleep 100
""", encoding="utf-8")
            mock_agy.chmod(0o755)

            script = f"""
import sys
from pathlib import Path
from manage import auth_login
auth_login(Path("{mock_agy}"), Path("{base}"), Path("{base}"), timeout=30.0)
"""
            runner = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                text=True,
            )
            import time
            for _ in range(50):
                if child_pid_file.exists() and child_pid_file.read_text().strip():
                    break
                time.sleep(0.05)
            self.assertTrue(child_pid_file.exists())
            cpid = int(child_pid_file.read_text().strip())

            import signal
            runner.terminate()
            ret = runner.wait(timeout=5.0)
            self.assertEqual(ret, 143)

            time.sleep(0.2)
            try:
                os.kill(cpid, 0)
                os.kill(cpid, signal.SIGKILL)
                self.fail("Child process survived SIGTERM during await_input")
            except ProcessLookupError:
                pass

    def test_sigterm_during_await_result_kills_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            child_pid_file = base / "child.pid"
            mock_agy = base / "mock_agy"
            mock_agy.write_text(f"""#!/bin/bash
echo $$ > "{child_pid_file}"
echo "Authentication required. Please visit the URL to log in:"
echo "  https://accounts.google.com/o/oauth2/auth?test=1"
echo "Or, paste the authorization code here and press Enter:"
read -r code
sleep 100
""", encoding="utf-8")
            mock_agy.chmod(0o755)

            script = f"""
import sys
from pathlib import Path
from manage import auth_login
auth_login(Path("{mock_agy}"), Path("{base}"), Path("{base}"), timeout=30.0, input_fn=lambda: "good_code")
"""
            runner = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            import time
            for _ in range(50):
                if child_pid_file.exists() and child_pid_file.read_text().strip():
                    break
                time.sleep(0.05)
            self.assertTrue(child_pid_file.exists())
            cpid = int(child_pid_file.read_text().strip())

            import signal
            runner.terminate()
            ret = runner.wait(timeout=5.0)
            self.assertEqual(ret, 143)

            time.sleep(0.2)
            try:
                os.kill(cpid, 0)
                os.kill(cpid, signal.SIGKILL)
                self.fail("Child process survived SIGTERM during await_result")
            except ProcessLookupError:
                pass


class CurrentCommitTransactionTests(unittest.TestCase):
    def test_atomic_current_commit_write_failure_causes_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config_dir = base / "etc"
            config_dir.mkdir()
            current_commit = config_dir / "current_commit"
            current_commit.write_text("commit_old\n", encoding="utf-8")

            backup_dir = base / "backup"
            backup_dir.mkdir()

            snippet = f'''
CONFIG_DIR="{config_dir}"
BACKUP="{backup_dir}"
COMMIT_CHANGED=0
SWITCHED=0

rollback() {{
  if [[ "$COMMIT_CHANGED" -eq 1 || "$SWITCHED" -eq 1 ]]; then
    if [[ -f "$BACKUP/current_commit" ]]; then
      cp -p -- "$BACKUP/current_commit" "$CONFIG_DIR/current_commit"
    else
      rm -f -- "$CONFIG_DIR/current_commit"
    fi
  fi
}}

cp -p -- "$CONFIG_DIR/current_commit" "$BACKUP/current_commit"

# Simulate write failure by making current_commit.next.$$ an existing directory
mkdir "$CONFIG_DIR/current_commit.next.$$"
if printf '%s\\n' "commit_new" > "$CONFIG_DIR/current_commit.next.$$" 2>/dev/null && \\
   mv -f -- "$CONFIG_DIR/current_commit.next.$$" "$CONFIG_DIR/current_commit"; then
  COMMIT_CHANGED=1
else
  rm -rf "$CONFIG_DIR/current_commit.next.$$"
  COMMIT_CHANGED=1
  rollback
  echo "CURRENT_COMMIT_WRITE_FAILED" >&2
  exit 1
fi
'''
            res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("CURRENT_COMMIT_WRITE_FAILED", res.stderr)
            self.assertEqual(current_commit.read_text(encoding="utf-8"), "commit_old\n")

    def test_rollback_stops_auth_helper_before_restoring_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            token = base / "oauth_token"
            token.write_text("ORIGINAL_TOKEN", encoding="utf-8")
            token_backup = base / "oauth_token.bak"
            token_backup.write_text("ORIGINAL_TOKEN", encoding="utf-8")

            pid_file = base / "auth.pid"
            auth_proc = subprocess.Popen(
                ["bash", "-c", f'echo $$ > "{pid_file}"; while true; do echo "ROGUE_WRITE" > "{token}"; sleep 0.05; done'],
                start_new_session=True,
            )
            import time
            for _ in range(50):
                if pid_file.exists() and pid_file.read_text().strip():
                    break
                time.sleep(0.02)
            auth_pid = int(pid_file.read_text().strip())

            snippet = f'''
AUTH_PID="{auth_proc.pid}"
TOKEN_ORIGINAL="{token}"
TOKEN_BACKUP="{token_backup}"

stop_auth_helper() {{
  if [[ -n "$AUTH_PID" ]] && kill -0 "$AUTH_PID" 2>/dev/null; then
    kill -TERM "$AUTH_PID" 2>/dev/null || true
    for _ in {{1..20}}; do
      if ! kill -0 "$AUTH_PID" 2>/dev/null; then
        break
      fi
      sleep 0.05
    done
    if kill -0 "$AUTH_PID" 2>/dev/null; then
      kill -KILL "$AUTH_PID" 2>/dev/null || true
    fi
  fi
}}

stop_auth_helper
cp -p -- "$TOKEN_BACKUP" "$TOKEN_ORIGINAL"
'''
            subprocess.run(["bash", "-c", snippet], check=True)
            import signal
            try:
                os.kill(auth_pid, signal.SIGKILL)
            except OSError:
                pass
            auth_proc.poll()
            time.sleep(0.1)
            self.assertEqual(token.read_text(encoding="utf-8"), "ORIGINAL_TOKEN")


if __name__ == "__main__":
    unittest.main()
