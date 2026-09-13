"""Menu and prompt-order regressions. No install, sudo or account changes are run."""
from __future__ import annotations

import argparse
import contextlib
import errno
import io
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import manage
from settings import Settings


class InstallMenuTests(unittest.TestCase):
    def menu(self, text: str, prefix: str = "") -> subprocess.CompletedProcess:
        script = prefix + '\nsource "$1"; choose_operation; printf "\\nMODE=%s\\n" "$MODE"'
        return subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh")],
                              input=text, capture_output=True, text=True, timeout=5)

    def test_install_choice(self):
        r = self.menu("1\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=install", r.stdout)
        self.assertNotIn("Token", r.stdout)
        self.assertNotIn("Google", r.stdout)

    def test_purge_uninstall_choice(self):
        r = self.menu("3\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=uninstall", r.stdout)

    def test_zero_choice(self):
        r = self.menu("0\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=exit", r.stdout)

    def test_blank_reprompts_not_default_install(self):
        r = self.menu("\n0\n")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("[0/1/2/3]"), 2)
        self.assertIn("MODE=exit", r.stdout)

    def test_invalid_reprompts(self):
        r = self.menu("4\nwrong\n1\n")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("[0/1/2/3]"), 3)
        self.assertIn("MODE=install", r.stdout)

    def test_eof_cancels(self):
        r = self.menu("")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=exit", r.stdout)

    def test_incomplete_line_at_eof_does_not_select(self):
        r = self.menu("2")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=exit", r.stdout)

    def test_read_error_is_not_reported_as_success(self):
        r = self.menu("", 'read() { return 2; }')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("read", r.stderr)
        self.assertNotIn("MODE=install", r.stdout)

    def test_help_noninteractive_has_no_menu(self):
        r = subprocess.run(["bash", str(ROOT / "install.sh"), "--help"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--install", r.stdout)
        self.assertNotIn("[0/1/2/3]", r.stdout)

    def run_main_until_privilege_boundary(self, args=(), text=""):
        # Stop at the privilege/deploy boundary, before any real system operation.
        with tempfile.TemporaryDirectory() as temp:
            sudo = Path(temp) / "sudo"
            sudo.write_text('#!/bin/bash\nprintf "FAKE_SUDO"\nprintf " <%s>" "$@"\n'
                            'printf "\\n"\nexit 88\n')
            sudo.chmod(0o755)
            wrapper = r'''
source "$1"
export PATH="$2:$PATH"
shift 2
acquire_deploy_lock() {
  printf 'BOUNDARY mode=%s purge=%s auto=%s reauth=%s ref=%s\n' "$MODE" "$PURGE" "$ENABLE_AUTO" "$REAUTH" "$REF"
  exit 88
}
main "$@"
'''
            master, slave = pty.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            proc = subprocess.Popen(["bash", "-c", wrapper, "_", str(ROOT / "install.sh"),
                                     temp, *args], stdin=slave, stdout=slave, stderr=slave,
                                    close_fds=True, start_new_session=True)
            os.close(slave)
            output = bytearray()
            try:
                if text:
                    os.write(master, text.encode())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if ready:
                        try:
                            data = os.read(master, 65536)
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                break
                            raise
                        if not data:
                            break
                        output.extend(data)
                    elif proc.poll() is not None:
                        break
                else:
                    self.fail("Menu did not finish at the mocked boundary within 5 seconds")
                return proc.wait(timeout=2), output.decode(errors="replace")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)
                os.close(master)

    def assert_mode_at_boundary(self, rc, output, mode):
        self.assertEqual(rc, 88, output)
        if os.geteuid() == 0:
            self.assertIn(f"BOUNDARY mode={mode}", output)
        else:
            self.assertIn("FAKE_SUDO", output)
            self.assertTrue(output.rstrip().endswith(f"<--{mode}>"), output)

    def test_main_zero_no_sudo_or_lock(self):
        rc, output = self.run_main_until_privilege_boundary(text="0\n")
        self.assertEqual(rc, 0, output)
        self.assertNotIn("FAKE_SUDO", output)
        self.assertNotIn("BOUNDARY", output)

    def test_main_one_routes_install_once(self):
        rc, output = self.run_main_until_privilege_boundary(text="1\n")
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertEqual(output.count("[0/1/2/3]"), 1)

    def test_main_two_routes_update_once(self):
        rc, output = self.run_main_until_privilege_boundary(text="2\n")
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertEqual(output.count("[0/1/2/3]"), 1)

    def test_main_three_routes_purge_uninstall_once(self):
        rc, output = self.run_main_until_privilege_boundary(text="3\n")
        self.assert_mode_at_boundary(rc, output, "uninstall")
        self.assertIn("purge=1" if os.geteuid() == 0 else "<--purge>", output)
        self.assertEqual(output.count("[0/1/2/3]"), 1)

    def test_main_explicit_install_skips_menu(self):
        rc, output = self.run_main_until_privilege_boundary(("--install",))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3]", output)

    def test_main_explicit_uninstall_skips_menu(self):
        rc, output = self.run_main_until_privilege_boundary(("--uninstall",))
        self.assert_mode_at_boundary(rc, output, "uninstall")
        self.assertNotIn("[0/1/2/3]", output)

    def test_main_auto_approve_shortcut_preserved(self):
        rc, output = self.run_main_until_privilege_boundary(("--enable-auto-approve",))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3]", output)
        self.assertIn("auto=1" if os.geteuid() == 0 else "<--enable-auto-approve>", output)

    def test_main_reauth_shortcut_preserved(self):
        rc, output = self.run_main_until_privilege_boundary(("--reauth",))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3]", output)
        self.assertIn("reauth=1" if os.geteuid() == 0 else "<--reauth>", output)

    def test_main_ref_main_shortcut_preserved(self):
        rc, output = self.run_main_until_privilege_boundary(("--ref", "main"))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3]", output)

    def test_main_purge_without_uninstall_still_rejected(self):
        rc, output = self.run_main_until_privilege_boundary(("--purge",))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("BOUNDARY", output)
        self.assertNotIn("FAKE_SUDO", output)
        self.assertNotIn("[0/1/2/3]", output)

    def test_main_confirmed_purge_route_keeps_uninstall(self):
        rc, output = self.run_main_until_privilege_boundary(("--uninstall", "--purge"))
        self.assert_mode_at_boundary(rc, output, "uninstall")
        self.assertNotIn("[0/1/2/3]", output)

    def test_unknown_flag_rejected_before_menu(self):
        rc, output = self.run_main_until_privilege_boundary(("--unknown",))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("BOUNDARY", output)
        self.assertNotIn("FAKE_SUDO", output)
        self.assertNotIn("[0/1/2/3]", output)


class WizardOrderTests(unittest.TestCase):
    def test_second_prompt_waits_for_first_and_token_is_not_printed(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "candidate"
            dest.touch()
            args = argparse.Namespace(old=None, output=dest, home="/root", enable_auto=False)
            output = io.StringIO()
            completed = []
            fixture_token = "123456:" + "x" * 32

            def prompt_side_effect(_=None):
                if not completed:
                    self.assertIn("1/3", output.getvalue())
                    self.assertNotIn("2/3", output.getvalue())
                    self.assertNotIn("3/3", output.getvalue())
                    completed.append(1)
                    return fixture_token
                self.assertEqual(completed, [1])
                self.assertIn("2/3", output.getvalue())
                self.assertNotIn("3/3", output.getvalue())
                completed.append(2)
                return "12345"

            with contextlib.redirect_stdout(output), patch("builtins.input", side_effect=prompt_side_effect):
                self.assertEqual(manage.prepare_config(args), 0)
            self.assertEqual(completed, [1, 2])
            self.assertEqual(Settings.load(dest).allowed, frozenset({12345}))
            self.assertNotIn(fixture_token, output.getvalue())

    def test_cancel_first_input_never_shows_second(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "candidate"
            dest.touch()
            argv = ["manage.py", "prepare-config", "--output", str(dest), "--home", "/root"]
            output = io.StringIO()
            call_count = []

            def cancel_first(_=None):
                call_count.append(1)
                raise EOFError

            with patch("sys.argv", argv), patch("builtins.input", side_effect=cancel_first), \
                    contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(manage.main(), 20)
            self.assertEqual(len(call_count), 1)
            self.assertNotIn("2/3", output.getvalue())
            self.assertEqual(dest.read_text(), "")

    def test_only_three_step_headers_remain(self):
        text = (ROOT / "install.sh").read_text() + (ROOT / "manage.py").read_text()
        for number in range(1, 4):
            self.assertEqual(text.count(f"\u6b65\u9aa4 {number}/3"), 1)
        self.assertNotIn("\u6b65\u9aa4 4/", text)


if __name__ == "__main__":
    unittest.main()
