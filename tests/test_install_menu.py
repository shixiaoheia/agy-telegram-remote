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
        self.assertIn("antigravity.google/docs/faq", r.stdout)
        self.assertIn("账号资格", r.stdout)

    def test_purge_uninstall_choice(self):
        r = self.menu("3\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=uninstall", r.stdout)

    def test_proxy_choice(self):
        r = self.menu("4\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=proxy", r.stdout)

    def test_diagnose_choice(self):
        r = self.menu("5\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=diagnose", r.stdout)

    def test_diagnose_classifies_409_without_printing_log_content(self):
        script = r'''
source "$1"
CONFIG_DIR="$2"
systemctl() {
  case "$*" in
    "is-active agy-telegram-remote") echo failed; return 3 ;;
    *"MainPID"*) echo 0 ;;
    *"InvocationID"*) printf '%032d\n' 1 ;;
    *) return 1 ;;
  esac
}
journalctl() { printf 'startup_or_poll_failed: Telegram code=409 private-log-content\n'; }
diagnose
'''
        with tempfile.TemporaryDirectory() as temp:
            r = subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh"), temp],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Telegram 409", r.stdout)
        self.assertNotIn("private-log-content", r.stdout)

    def test_initial_proxy_choice(self):
        script = 'source "$1"; choose_initial_proxy; printf "NEW_PROXY=%s\\n" "$NEW_PROXY"'
        for answer, expected in (("y\n", "1"), ("n\n", "0")):
            r = subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh")],
                               input=answer, capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0)
            self.assertIn(f"NEW_PROXY={expected}", r.stdout)

    def bootstrap_proxy(self, symlink=False):
        with tempfile.TemporaryDirectory() as temp:
            script = r'''
source "$1"
RELEASES="$2/releases"
PROXY_TEST_MODE="$3"
mkdir -p "$RELEASES"
root_dir() { :; }
validate_release_files() { :; }
git() {
  case "$*" in
    *" init -q "*)
      PROXY_FAKE_STAGE="${@: -1}"
      mkdir "$PROXY_FAKE_STAGE/.git"
      ;;
    *" checkout -q "*)
      : > "$PROXY_FAKE_STAGE/proxy_manager.py"
      if [[ "$PROXY_TEST_MODE" == 'symlink' ]]; then
        ln -s /etc/passwd "$PROXY_FAKE_STAGE/proxy_node.py"
      else
        : > "$PROXY_FAKE_STAGE/proxy_node.py"
      fi
      ;;
    *" rev-parse HEAD"*) printf '%040d\n' 1 ;;
  esac
}
execute_proxy_helper() { test -f "$1/proxy_node.py"; printf 'BOOTSTRAP_HELPER_OK\n'; }
run_proxy_setup
'''
            r = subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh"), temp,
                                "symlink" if symlink else "normal"],
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(list((Path(temp) / "releases").iterdir()), [])
            return r

    def test_proxy_bootstraps_without_old_release(self):
        r = self.bootstrap_proxy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BOOTSTRAP_HELPER_OK", r.stdout)

    def test_proxy_bootstrap_rejects_symlink_before_execution(self):
        r = self.bootstrap_proxy(symlink=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("BOOTSTRAP_HELPER_OK", r.stdout)

    def test_proxy_bootstrap_uses_current_running_managed_proxy(self):
        with tempfile.TemporaryDirectory() as temp:
            script = r'''
source "$1"
RELEASES="$2/releases"
CONFIG_DIR="$2/config"
mkdir -p "$RELEASES" "$CONFIG_DIR"
: > "$CONFIG_DIR/proxy.env"
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
root_dir() { :; }
validate_release_files() { :; }
installed_proxy_helper() { return 1; }
systemctl() { return 0; }
load_managed_proxy() { export HTTPS_PROXY='http://127.0.0.1:32123'; }
git() {
  case "$*" in
    *" init -q "*) PROXY_FAKE_STAGE="${@: -1}"; mkdir "$PROXY_FAKE_STAGE/.git" ;;
    *" fetch "*) printf 'FETCH_PROXY=%s\n' "${HTTPS_PROXY-UNSET}" ;;
    *" checkout -q "*) : > "$PROXY_FAKE_STAGE/proxy_manager.py"; : > "$PROXY_FAKE_STAGE/proxy_node.py" ;;
    *" rev-parse HEAD"*) printf '%040d\n' 1 ;;
  esac
}
execute_proxy_helper() { :; }
run_proxy_setup
'''
            r = subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh"), temp],
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("FETCH_PROXY=http://127.0.0.1:32123", r.stdout)

    def test_proxy_management_can_recover_offline_with_installed_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            script = r'''
source "$1"
TEST_RELEASE="$2"
installed_proxy_helper() { printf '%s\n' "$TEST_RELEASE"; }
git() { printf 'UNEXPECTED_FETCH\n'; return 1; }
execute_proxy_helper() { printf 'OFFLINE_HELPER_USED\n'; }
run_proxy_setup
'''
            r = subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh"), temp],
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("OFFLINE_HELPER_USED", r.stdout)
            self.assertNotIn("UNEXPECTED_FETCH", r.stdout)

    def test_proxy_management_can_choose_fresh_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            script = r'''
source "$1"
RELEASES="$2/releases"
mkdir -p "$RELEASES"
TEST_RELEASE="$2"
installed_proxy_helper() { printf '%s\n' "$TEST_RELEASE"; }
root_dir() { :; }
validate_release_files() { :; }
git() {
  case "$*" in
    *" init -q "*) stage="${@: -1}"; mkdir "$stage/.git" ;;
    *" fetch "*) echo FETCH_SELECTED ;;
    *" checkout -q "*) : > "$stage/proxy_manager.py"; : > "$stage/proxy_node.py" ;;
    *" rev-parse HEAD"*) printf '%040d\n' 1 ;;
  esac
}
execute_proxy_helper() { printf 'FRESH_HELPER_USED\n'; }
run_proxy_setup
'''
            r = subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh"), temp],
                               input="y\n", capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("FETCH_SELECTED", r.stdout)
            self.assertIn("FRESH_HELPER_USED", r.stdout)

    def test_install_blocks_on_unrecovered_proxy_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".proxy-transaction.json").touch()
            script = 'source "$1"; CONFIG_DIR="$2"; load_managed_proxy; printf "UNEXPECTED\\n"'
            r = subprocess.run(["bash", "-c", script, "_", str(ROOT / "install.sh"), temp],
                               capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("UNEXPECTED", r.stdout)
            self.assertIn("上次节点切换未完成", r.stderr)

    def test_zero_choice(self):
        r = self.menu("0\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("MODE=exit", r.stdout)

    def test_blank_reprompts_not_default_install(self):
        r = self.menu("\n0\n")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("[0/1/2/3/4/5]"), 2)
        self.assertIn("MODE=exit", r.stdout)

    def test_invalid_reprompts(self):
        r = self.menu("9\nwrong\n1\n")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("[0/1/2/3/4/5]"), 3)
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
        self.assertNotIn("[0/1/2/3/4/5]", r.stdout)

    def run_main_until_privilege_boundary(self, args=(), text="", existing=False, legacy=False):
        # Stop at the privilege/deploy boundary, before any real system operation.
        with tempfile.TemporaryDirectory() as temp:
            sudo = Path(temp) / "sudo"
            sudo.write_text('#!/bin/bash\nprintf "FAKE_SUDO"\nprintf " <%s>" "$@"\n'
                            'printf "\\n"\nexit 88\n')
            sudo.chmod(0o755)
            if existing:
                (Path(temp) / "config.env").touch()
            if legacy:
                (Path(temp) / "app").mkdir()
                (Path(temp) / "app/.env").touch()
            wrapper = r'''
source "$1"
export PATH="$2:$PATH"
CONFIG="$2/config.env"
APP="$2/app"
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
        self.assertEqual(output.count("[0/1/2/3/4/5]"), 1)
        self.assertIn("安装（推荐", output)

    def test_main_two_routes_update_once(self):
        rc, output = self.run_main_until_privilege_boundary(text="2\n")
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertEqual(output.count("[0/1/2/3/4/5]"), 1)

    def test_main_three_routes_purge_uninstall_once(self):
        rc, output = self.run_main_until_privilege_boundary(text="3\n")
        self.assert_mode_at_boundary(rc, output, "uninstall")
        self.assertIn("purge=1" if os.geteuid() == 0 else "<--purge>", output)
        self.assertEqual(output.count("[0/1/2/3/4/5]"), 1)

    def test_existing_install_shows_menu_before_update(self):
        rc, output = self.run_main_until_privilege_boundary(text="2\n", existing=True)
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertIn("[0/1/2/3/4/5]", output)
        self.assertIn("更新到最新版本（推荐", output)

    def test_legacy_install_shows_menu_before_update(self):
        rc, output = self.run_main_until_privilege_boundary(text="2\n", legacy=True)
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertIn("[0/1/2/3/4/5]", output)

    def test_explicit_menu_still_allows_exit_on_existing_install(self):
        rc, output = self.run_main_until_privilege_boundary(("--menu",), text="0\n", existing=True)
        self.assertEqual(rc, 0, output)
        self.assertNotIn("BOUNDARY", output)
        self.assertNotIn("FAKE_SUDO", output)

    def test_reconfigure_skips_menu(self):
        rc, output = self.run_main_until_privilege_boundary(("--reconfigure",))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3/4/5]", output)

    def test_main_explicit_install_skips_menu(self):
        rc, output = self.run_main_until_privilege_boundary(("--install",))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3/4/5]", output)

    def test_main_explicit_uninstall_skips_menu(self):
        rc, output = self.run_main_until_privilege_boundary(("--uninstall",))
        self.assert_mode_at_boundary(rc, output, "uninstall")
        self.assertNotIn("[0/1/2/3/4/5]", output)

    def test_main_auto_approve_shortcut_preserved(self):
        rc, output = self.run_main_until_privilege_boundary(("--enable-auto-approve",))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3/4/5]", output)
        self.assertIn("auto=1" if os.geteuid() == 0 else "<--enable-auto-approve>", output)

    def test_main_reauth_shortcut_preserved(self):
        rc, output = self.run_main_until_privilege_boundary(("--reauth",))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3/4/5]", output)
        self.assertIn("reauth=1" if os.geteuid() == 0 else "<--reauth>", output)

    def test_main_ref_main_shortcut_preserved(self):
        rc, output = self.run_main_until_privilege_boundary(("--ref", "main"))
        self.assert_mode_at_boundary(rc, output, "install")
        self.assertNotIn("[0/1/2/3/4/5]", output)

    def test_main_purge_without_uninstall_still_rejected(self):
        rc, output = self.run_main_until_privilege_boundary(("--purge",))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("BOUNDARY", output)
        self.assertNotIn("FAKE_SUDO", output)
        self.assertNotIn("[0/1/2/3/4/5]", output)

    def test_main_confirmed_purge_route_keeps_uninstall(self):
        rc, output = self.run_main_until_privilege_boundary(("--uninstall", "--purge"))
        self.assert_mode_at_boundary(rc, output, "uninstall")
        self.assertNotIn("[0/1/2/3/4/5]", output)

    def test_unknown_flag_rejected_before_menu(self):
        rc, output = self.run_main_until_privilege_boundary(("--unknown",))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("BOUNDARY", output)
        self.assertNotIn("FAKE_SUDO", output)
        self.assertNotIn("[0/1/2/3/4/5]", output)


class WizardOrderTests(unittest.TestCase):
    def test_second_prompt_waits_for_first_and_token_is_not_printed(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "candidate"
            dest.touch()
            args = argparse.Namespace(old=None, output=dest, home="/root", enable_auto=False)
            output = io.StringIO()
            completed = []
            fixture_token = "123456:" + "x" * 32

            def prompt_side_effect(_=None, **kwargs):
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

            with contextlib.redirect_stdout(output), \
                    patch("manage.choose_token_hidden", return_value=True), \
                    patch("manage.getpass.getpass", side_effect=prompt_side_effect), \
                    patch("builtins.input", side_effect=prompt_side_effect):
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

            def cancel_first(_=None, **kwargs):
                call_count.append(1)
                raise EOFError

            with patch("sys.argv", argv), patch("manage.choose_token_hidden", return_value=True), \
                    patch("manage.getpass.getpass", side_effect=cancel_first), \
                    contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(manage.main(), 20)
            self.assertEqual(len(call_count), 1)
            self.assertNotIn("2/3", output.getvalue())
            self.assertEqual(dest.read_text(), "")

    def check_real_terminal_token_visibility(self, choice, visible):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "candidate"
            dest.touch()
            master, slave = pty.openpty()
            proc = subprocess.Popen([sys.executable, str(ROOT / "manage.py"), "prepare-config",
                                     "--output", str(dest)], stdin=slave, stdout=slave, stderr=slave,
                                    close_fds=True, start_new_session=True)
            os.close(slave)
            output = bytearray()
            fixture_token = "123456:" + "x" * 32
            try:
                def read_until(marker):
                    deadline = time.monotonic() + 5
                    while marker not in output and time.monotonic() < deadline:
                        ready, _, _ = select.select([master], [], [], 0.1)
                        if ready:
                            try:
                                output.extend(os.read(master, 65536))
                            except OSError:
                                break
                    self.assertIn(marker, output)
                read_until(b"[Y/n]")
                os.write(master, (choice + "\n").encode())
                read_until(b"Token> ")
                os.write(master, (fixture_token + "\n").encode())
                read_until("步骤 2/3".encode())
                os.write(master, b"12345\n")
                self.assertEqual(proc.wait(timeout=5), 0)
                while select.select([master], [], [], 0)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
                if visible:
                    self.assertIn(fixture_token.encode(), output)
                else:
                    self.assertNotIn(fixture_token.encode(), output)
                self.assertEqual(Settings.load(dest).allowed, frozenset({12345}))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2)
                os.close(master)

    def test_real_terminal_hides_token_by_default(self):
        self.check_real_terminal_token_visibility("", False)

    def test_real_terminal_can_show_token(self):
        self.check_real_terminal_token_visibility("n", True)

    def test_only_three_step_headers_remain(self):
        text = (ROOT / "install.sh").read_text() + (ROOT / "manage.py").read_text()
        for number in range(1, 4):
            self.assertEqual(text.count(f"\u6b65\u9aa4 {number}/3"), 1)
        self.assertNotIn("\u6b65\u9aa4 4/", text)


if __name__ == "__main__":
    unittest.main()
