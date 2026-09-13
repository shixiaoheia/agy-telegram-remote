"""Offline regression tests for the September 2026 CI environment failures."""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pty
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

from agy_runner import Result, classify
from manage import _read_code_line, auth_login, classify_auth_error, smoke

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


if __name__ == "__main__":
    unittest.main()
