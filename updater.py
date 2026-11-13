"""Owner-triggered bot updates in a separate systemd service, never a model task."""
from __future__ import annotations

import asyncio
import fcntl
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from agy_runner import child_environment
from settings import Settings, check_no_symlink
from state_store import atomic_json, read_json
from telegram_api import TelegramAPI

SERVICE = "agy-telegram-update.service"
REPO = "https://github.com/shixiaoheia/agy-telegram-remote.git"
BRANCH = "refs/heads/main"
APP = Path("/opt/agy-telegram-remote")
RELEASES = Path("/opt/agy-telegram-remote-releases")
CONFIG = Path("/etc/agy-telegram-remote/config.env")
TERMINAL = frozenset({"current", "success", "retained", "rolled_back", "failed", "unavailable"})
PHASES = TERMINAL | {"requested", "checking", "installing"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUEST = re.compile(r"[0-9a-f]{32}\Z")


def unit_text() -> str:
    return """# Managed by agy-telegram-remote; do not edit.
[Unit]
Description=Update Antigravity Telegram Remote from its fixed GitHub main branch
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=/opt/agy-telegram-remote
Environment=HOME=/root
EnvironmentFile=-/etc/agy-telegram-remote/proxy.env
ExecStart=/usr/bin/python3 -E -s -B /opt/agy-telegram-remote/updater.py apply
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
KillMode=control-group
TimeoutStartSec=20min
TimeoutStopSec=90
StandardOutput=null
StandardError=null
"""


def read_status(directory: Path) -> dict:
    try:
        value = read_json(directory / "update-status.json", 4096)
    except FileNotFoundError:
        return {}
    if (not isinstance(value, dict) or not isinstance(value.get("request_id"), str)
            or not REQUEST.fullmatch(value["request_id"]) or not isinstance(value.get("phase"), str)
            or value["phase"] not in PHASES):
        raise ValueError("Invalid update status")
    for key in ("current", "target", "actual"):
        if value.get(key) and (not isinstance(value[key], str) or not SHA.fullmatch(value[key])):
            raise ValueError("Invalid update revision")
    return value


def status_text(status: dict, language: str = "zh") -> str:
    phase = status.get("phase")
    zh = {"requested": "已请求更新", "checking": "正在检查 GitHub 新版本", "installing": "正在检查、安装并重启",
          "current": "✅ 已是最新版本，无需更新", "success": "✅ 更新完成，机器人已启动就绪",
          "retained": "⚠️ 更新未完成，原版本仍在运行", "rolled_back": "⚠️ 新版未通过验证，已恢复原版本",
          "failed": "⚠️ 更新失败，服务恢复尚未确认，请检查 VPS",
          "unavailable": "⚠️ 无法启动更新服务，请先在 VPS 安装一次支持 /update 的版本"}
    en = {"requested": "Update requested", "checking": "Checking GitHub for an update", "installing": "Checking, installing and restarting",
          "current": "✅ Already up to date", "success": "✅ Updated; the bot is ready",
          "retained": "⚠️ Update did not complete; the original version is running",
          "rolled_back": "⚠️ New version failed verification; the original version was restored",
          "failed": "⚠️ Update failed; service recovery is unconfirmed. Check the VPS",
          "unavailable": "⚠️ Updater unavailable. Install a version with /update support on the VPS once first"}
    english = language == "en"
    text = (en if english else zh).get(phase, "No update record" if english else "尚无更新记录")
    for key, a, b in (("current", "原版本", "Previous"), ("target", "目标版本", "Target"), ("actual", "当前版本", "Current")):
        if status.get(key):
            text += f"\n{b if english else a}: {status[key][:12]}"
    if phase in TERMINAL:
        text += "\n/status to verify" if english else "\n可发送 /status 查看部署版本"
    return text


async def systemctl(*args: str) -> int:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl", *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        return await asyncio.wait_for(process.wait(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def trusted(path: Path, *, directory: bool = False) -> None:
    check_no_symlink(path)
    info = path.stat()
    if (info.st_uid != 0 or info.st_mode & 0o022
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("Update source must be root-owned and immutable to other users")


def current_revision(app: Path = APP, releases: Path = RELEASES) -> tuple[Path, str]:
    # The public entry is the only expected symlink; all real path components
    # and the selected installed code must remain root controlled.
    trusted(app.parent, directory=True)
    trusted(releases, directory=True)
    if not app.is_symlink():
        raise ValueError("Updates require a managed release installation")
    release = app.resolve(strict=True)
    if release.parent != releases:
        raise ValueError("Release points outside the managed directory")
    trusted(release, directory=True)
    for name in ("install.sh", "updater.py", ".commit"):
        trusted(release / name)
    raw = (release / ".commit").read_bytes()
    if len(raw) > 80:
        raise ValueError("Invalid release marker")
    commit = raw.decode("ascii").strip()
    if not SHA.fullmatch(commit):
        raise ValueError("Invalid release revision")
    return release, commit


def worker_environment(settings: Settings) -> dict[str, str]:
    return child_environment(settings.home) | {
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
    }


def latest_revision(env: dict[str, str]) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never",
         "ls-remote", "--exit-code", REPO, BRANCH], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=True)
    if len(result.stdout) > 4096:
        raise ValueError("Oversized revision response")
    columns = result.stdout.decode("ascii").strip().split()
    if len(columns) != 2 or not SHA.fullmatch(columns[0]) or columns[1] != BRANCH:
        raise ValueError("Invalid revision response")
    return columns[0]


def install_revision(release: Path, target: str, env: dict[str, str]) -> int:
    if not SHA.fullmatch(target):
        raise ValueError("Only a pinned revision may be installed")
    # The installed, root-owned installer owns the shared deployment lock,
    # pre-stop validation, atomic release switch and ordinary-failure rollback.
    process = subprocess.Popen(
        ["/bin/bash", str(release / "install.sh"), "--bot-update", "--ref", target],
        env=env, cwd=release, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait(timeout=900)
    except BaseException:
        # TERM lets the installer's trap restore the previous release first.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=75)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        raise


def healthy() -> bool:
    try:
        result = subprocess.run(["/usr/bin/systemctl", "show", "agy-telegram-remote", "-p", "MainPID", "--value"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True)
        pid = int(result.stdout)
        value = read_json(Path("/run/agy-telegram-remote/ready.json"), 1024)
        return (isinstance(value, dict) and pid > 0 and value.get("pid") == pid and value.get("initialized") is True
                and value.get("polling_ready") is True
                and 0 <= time.time() - value.get("last_poll_at", 0) <= 90)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


async def notify(settings: Settings, status: dict, language: str) -> None:
    api = TelegramAPI(settings.token)
    try:
        await api.send(settings.owner_id, status_text(status, language))
    finally:
        api.close()


def apply(settings: Settings, *, app: Path = APP, releases: Path = RELEASES) -> int:
    request = read_json(settings.state_dir / "update-request.json", 1024)
    nonce = request.get("request_id") if isinstance(request, dict) else None
    stamp = request.get("created_at") if isinstance(request, dict) else None
    if (not isinstance(nonce, str) or not REQUEST.fullmatch(nonce)
            or type(stamp) not in {int, float} or not 0 <= time.time() - stamp <= 900):
        raise ValueError("Invalid or expired update request")
    previous = read_status(settings.state_dir)
    if previous.get("request_id") == nonce and previous.get("phase") in TERMINAL:
        return 0  # Never replay a completed request after a service restart.
    language = "en" if request.get("language") == "en" else "zh"
    status = {"request_id": nonce, "phase": "checking", "updated_at": time.time()}
    destination = settings.state_dir / "update-status.json"
    atomic_json(destination, status)
    original = ""
    try:
        trusted(CONFIG)
        release, original = current_revision(app, releases)
        status["current"] = original
        env = worker_environment(settings)
        target = latest_revision(env)
        status["target"] = target
        if target == original:
            status.update(phase="current", actual=original)
        else:
            status.update(phase="installing", updated_at=time.time())
            atomic_json(destination, status)
            code = install_revision(release, target, env)
            _, actual = current_revision(app, releases)
            status["actual"] = actual
            if code == 0 and actual == target and healthy():
                status["phase"] = "success"
            elif actual == original and healthy():
                status["phase"] = "rolled_back"
            else:
                status["phase"] = "failed"
    except Exception:
        # Do not persist or send exception text, installer output, URLs or keys.
        try:
            _, actual = current_revision(app, releases)
            status["actual"] = actual
            status["phase"] = "retained" if original and actual == original and healthy() else "failed"
        except Exception:
            status["phase"] = "failed"
    status["updated_at"] = time.time()
    atomic_json(destination, status)
    try:
        asyncio.run(notify(settings, status, language))
    except Exception:
        pass  # The durable result remains available through /update status.
    return 0 if status["phase"] in {"current", "success"} else 1


def main() -> int:
    if sys.argv[1:] == ["unit"]:
        print(unit_text(), end="")
        return 0
    if sys.argv[1:] != ["apply"] or os.geteuid() != 0:
        return 1
    try:
        trusted(CONFIG)
        settings = Settings.load(CONFIG)
        trusted(settings.state_dir, directory=True)
        def interrupted(_signum, _frame):
            raise RuntimeError("Update interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        lock = os.open(settings.state_dir / "update-worker.lock",
                       os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
                return 1
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return apply(settings)
        finally:
            os.close(lock)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
