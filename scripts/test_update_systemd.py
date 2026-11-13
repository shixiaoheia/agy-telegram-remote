#!/usr/bin/env python3
"""Prove updater handoff survives bot shutdown in a disposable CI VM only."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from updater import unit_text
from manage import service_unit


def main() -> None:
    if (os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("CI") != "true" or os.environ.get("AGY_TEST_ISOLATED_SYSTEMD") != "1"
            or Path("/proc/1/comm").read_text().strip() != "systemd"):
        raise SystemExit("Requires an explicitly isolated GitHub Actions VM with systemd and root.")
    prefix = f"agy-update-ci-{os.getpid()}"
    bot = prefix + "-bot.service"
    worker = prefix + "-worker.service"
    units = [Path("/etc/systemd/system") / name for name in (bot, worker)]
    if any(p.exists() or p.is_symlink() for p in units):
        raise SystemExit("Refusing existing test units")

    def ctl(*args, check=True):
        return subprocess.run(["/usr/bin/systemctl", *args], check=check, timeout=30,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with tempfile.TemporaryDirectory(prefix=prefix, dir="/run") as directory:
        base = Path(directory)
        bot_code = base / "bot_fixture.py"
        worker_code = base / "worker_fixture.py"
        bot_code.write_text(
            "import pathlib,subprocess,time\n"
            f"r=subprocess.run(['/usr/bin/systemctl','start','--no-block',{worker!r}])\n"
            f"pathlib.Path({str(base / 'requested')!r}).write_text(str(r.returncode))\n"
            "time.sleep(60)\n")
        worker_code.write_text(
            "import pathlib,subprocess,time\n"
            # Let the requesting restricted bot finish systemctl's response.
            "time.sleep(0.5)\n"
            f"subprocess.run(['/usr/bin/systemctl','stop',{bot!r}],check=True)\n"
            "time.sleep(0.5)\n"
            f"pathlib.Path({str(base / 'survived')!r}).write_text('ready')\n")
        restricted = service_unit(base, base, base)
        restricted = restricted.replace("WorkingDirectory=/opt/agy-telegram-remote", f"WorkingDirectory={base}")
        restricted = restricted.replace(
            "ExecStart=/usr/bin/python3 -E -s -B /opt/agy-telegram-remote/bot.py --config /etc/agy-telegram-remote/config.env --ready-file /run/agy-telegram-remote/ready.json",
            f"ExecStart=/usr/bin/python3 -I -B {bot_code}")
        restricted = restricted.replace("RuntimeDirectory=agy-telegram-remote", f"RuntimeDirectory={prefix}")
        restricted = restricted.replace("Restart=on-failure", "Restart=no")
        external = unit_text().replace("WorkingDirectory=/opt/agy-telegram-remote", f"WorkingDirectory={base}")
        external = external.replace("EnvironmentFile=-/etc/agy-telegram-remote/proxy.env\n", "")
        external = external.replace("ExecStart=/usr/bin/python3 -E -s -B /opt/agy-telegram-remote/updater.py apply",
                                    f"ExecStart=/usr/bin/python3 -I -B {worker_code}")
        try:
            units[0].write_text(restricted)
            units[1].write_text(external)
            ctl("daemon-reload")
            ctl("start", bot)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (base / "survived").exists():
                time.sleep(0.1)
            assert (base / "requested").read_text() == "0", "Restricted bot could not hand off"
            assert (base / "survived").read_text() == "ready", "Updater died when the bot stopped"
            assert ctl("is-active", "--quiet", bot, check=False).returncode != 0
            print("Updater handoff passed: restricted bot stopped, independent worker survived")
            # Validate internal headless flags under real root without invoking main.
            subprocess.run(["/bin/bash", "-c", '''
source "$1"
CONFIG="$2/config"; UNIT="$2/unit"; APP="$2/app"
touch "$CONFIG" "$UNIT"; ln -s "$2" "$APP"
MODE=install; REF=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
validate_bot_update
REAUTH=1
if (validate_bot_update); then exit 1; fi
''', "_", str(ROOT / "install.sh"), str(base)], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("Pinned noninteractive update flags passed")
            # Run the installed self-check launcher under actual root, while
            # the fixture proves it cannot write root-owned deployment files.
            base.chmod(0o755)
            fixture = base / "offline-fixture"
            fixture.mkdir(mode=0o755)
            tests = fixture / "tests"
            tests.mkdir(mode=0o755)
            protected = fixture / "production-marker"
            protected.write_text("preserve")
            protected.chmod(0o644)
            test = tests / "test_unprivileged.py"
            test.write_text(
                "import os,pathlib,sys,unittest\n"
                "class Identity(unittest.TestCase):\n"
                " def test_unprivileged(self):\n"
                "  self.assertNotEqual(os.geteuid(),0)\n"
                "  self.assertNotIn('TELEGRAM_BOT_TOKEN',os.environ)\n"
                "  self.assertEqual(sys.stdin.read(1),'')\n"
                f"  with self.assertRaises(PermissionError): pathlib.Path({str(protected)!r}).write_text('bad')\n")
            test.chmod(0o644)
            subprocess.run(["/bin/bash", "-c", 'source "$1"; run_offline_tests "$2"',
                            "_", str(ROOT / "install.sh"), str(fixture)], check=True, timeout=15,
                           input="installer-answer\n", text=True)
            assert protected.read_text() == "preserve"
            print("Installer offline self-check runs unprivileged and preserves root files")
        finally:
            for name in (bot, worker):
                ctl("stop", name, check=False)
                ctl("reset-failed", name, check=False)
            for path in units:
                path.unlink(missing_ok=True)
            ctl("daemon-reload", check=False)


if __name__ == "__main__":
    main()
