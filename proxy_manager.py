#!/usr/bin/env python3
"""Manage the project's private, loopback-only sing-box proxy.

Node secrets are read with terminal echo disabled and never passed on a command
line. The surrounding installer holds its deployment lock before invoking us.
"""
from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import warnings
from pathlib import Path

from proxy_node import NodeError, build_config, parse_node

VERSION = "1.14.4"
ARCHES = {
    "x86_64": ("amd64", "699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968"),
    "aarch64": ("arm64", "436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26"),
}
CONFIG_DIR = Path("/etc/agy-telegram-remote")
ENGINE_DIR = Path("/opt/agy-telegram-remote-proxy")
CONFIG = CONFIG_DIR / "proxy.json"
ENV = CONFIG_DIR / "proxy.env"
UNIT = Path("/etc/systemd/system/agy-telegram-proxy.service")
DROP_DIR = Path("/etc/systemd/system/agy-telegram-remote.service.d")
DROP = DROP_DIR / "90-agy-managed-proxy.conf"
ENGINE = ENGINE_DIR / "sing-box"
ENGINE_MARKER = ENGINE_DIR / ".agy-managed"
SERVICE = "agy-telegram-remote.service"
PROXY_SERVICE = "agy-telegram-proxy.service"
MARKER = "# Managed by agy-telegram-remote; do not edit.\n"


class ProxyError(Exception):
    pass


def secure_dir(path: Path, mode: int) -> None:
    """Reject links and directories writable by anyone other than root."""
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ProxyError(f"目录不安全：{path}")
    else:
        path.mkdir(mode=mode)


def read_managed(path: Path, marker: bool = False) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & (0o077 if path in (CONFIG, ENV) else 0o022)
                or info.st_size > 1024 * 1024):
            raise ProxyError(f"文件不安全：{path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(1024 * 1024 + 1)
        if marker and not data.startswith(MARKER.encode()):
            raise ProxyError(f"拒绝覆盖非本项目管理的文件：{path}")
        return data
    finally:
        os.close(fd)


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    read_managed(path)
    fd, name = tempfile.mkstemp(prefix=".agy-proxy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def run(*args: str, check: bool = True, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout, check=check)


def active(name: str) -> bool:
    return run("systemctl", "is-active", "--quiet", name, check=False).returncode == 0


def enabled(name: str) -> bool:
    return run("systemctl", "is-enabled", "--quiet", name, check=False).returncode == 0


def install_engine() -> None:
    secure_dir(ENGINE_DIR, 0o755)
    marker = read_managed(ENGINE_MARKER)
    if marker is None:
        if any(ENGINE_DIR.iterdir()):
            raise ProxyError("已有代理核心目录不是本项目管理的目录。")
        atomic_write(ENGINE_MARKER, b"agy-telegram-proxy-v1\n", 0o600)
    elif marker != b"agy-telegram-proxy-v1\n":
        raise ProxyError("代理核心目录标记不正确。")
    if ENGINE.exists() or ENGINE.is_symlink():
        info = ENGINE.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o022 or not info.st_mode & 0o100):
            raise ProxyError("已有代理核心文件不安全。")
        return
    arch = os.uname().machine
    if arch not in ARCHES:
        raise ProxyError("自动安装代理核心仅支持 x86_64 和 ARM64。")
    suffix, expected = ARCHES[arch]
    archive_name = f"sing-box-{VERSION}-linux-{suffix}.tar.gz"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{VERSION}/{archive_name}"
    print("正在下载并校验代理核心……")
    fd, name = tempfile.mkstemp(prefix=".download-", dir=ENGINE_DIR)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as target, urllib.request.urlopen(url, timeout=60) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ProxyError("代理核心下载文件异常。")
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != expected:
            raise ProxyError("代理核心校验失败，已停止。")
        with tarfile.open(name, "r:gz") as archive:
            member = archive.getmember(f"sing-box-{VERSION}-linux-{suffix}/sing-box")
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise ProxyError("代理核心归档格式无效。")
            source = archive.extractfile(member)
            if source is None:
                raise ProxyError("代理核心归档格式无效。")
            fd2, executable = tempfile.mkstemp(prefix=".engine-", dir=ENGINE_DIR)
            try:
                with os.fdopen(fd2, "wb") as out:
                    shutil.copyfileobj(source, out)
                    os.fchmod(out.fileno(), 0o755)
                os.replace(executable, ENGINE)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(executable)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check_connectivity(port: int) -> None:
    for url in ("https://antigravity.google/", "https://api.telegram.org/"):
        result = run("curl", "--silent", "--show-error", "--output", "/dev/null",
                     "--max-time", "20", "--noproxy", "", "--proxy",
                     f"http://127.0.0.1:{port}", url, check=False, timeout=25)
        if result.returncode:
            raise ProxyError("节点无法连接 Google 或 Telegram，请检查节点和网络。")


def wait_listener(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise ProxyError("代理服务未能在本机端口就绪。")


def probe(config: dict, port: int) -> None:
    """Check syntax and TLS connectivity without sending bot credentials."""
    fd, name = tempfile.mkstemp(prefix=".proxy-probe-", dir=CONFIG_DIR)
    proc = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
        run(str(ENGINE), "check", "-c", name)
        proc = subprocess.Popen([str(ENGINE), "run", "-c", name], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise ProxyError("节点代理无法启动。")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise ProxyError("节点代理未能监听本地端口。")
        check_connectivity(port)
        if proc.poll() is not None:
            raise ProxyError("节点代理已退出。")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def unit_text() -> bytes:
    return (MARKER + f"""[Unit]
Description=AGY Telegram Remote local proxy
After=network-online.target
Wants=network-online.target
[Service]
ExecStart={ENGINE} run -c {CONFIG}
Restart=on-failure
RestartSec=5
User=root
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
[Install]
WantedBy=multi-user.target
""").encode()


def env_text(port: int) -> bytes:
    proxy = f"http://127.0.0.1:{port}"
    return (f"HTTP_PROXY={proxy}\nHTTPS_PROXY={proxy}\n"
            f"http_proxy={proxy}\nhttps_proxy={proxy}\n"
            "NO_PROXY=localhost,127.0.0.1,::1\nno_proxy=localhost,127.0.0.1,::1\n"
            "ALL_PROXY=\nall_proxy=\n").encode()


def drop_text() -> bytes:
    return (MARKER + f"[Unit]\nWants={PROXY_SERVICE}\nAfter={PROXY_SERVICE}\n"
            f"[Service]\nEnvironmentFile={ENV}\n").encode()


def ready() -> bool:
    result = subprocess.run(["systemctl", "show", SERVICE, "-p", "MainPID", "--value"],
                            capture_output=True, text=True, timeout=10, check=False)
    pid = result.stdout.strip()
    if not pid.isdecimal() or pid == "0":
        return False
    return run("/usr/bin/python3", "-E", "-s", "-B", "/opt/agy-telegram-remote/manage.py",
               "check-ready", "--file", "/run/agy-telegram-remote/ready.json", "--pid", pid,
               check=False, timeout=10).returncode == 0


def service_proxy_matches(port: int) -> bool:
    result = subprocess.run(["systemctl", "show", SERVICE, "-p", "MainPID", "--value"],
                            capture_output=True, text=True, timeout=10, check=False)
    pid = result.stdout.strip()
    if not pid.isdecimal() or pid == "0":
        return False
    try:
        data = (Path("/proc") / pid / "environ").read_bytes()
    except OSError:
        return False
    if len(data) > 1024 * 1024:
        return False
    entries = data.split(b"\0")
    proxy = f"http://127.0.0.1:{port}".encode()
    for key in (b"HTTP_PROXY", b"HTTPS_PROXY", b"http_proxy", b"https_proxy"):
        values = [entry[len(key) + 1:] for entry in entries if entry.startswith(key + b"=")]
        if values != [proxy]:
            return False
    return True


def apply(config: dict, port: int) -> None:
    """Switch only after a candidate probe. Roll back both services on failure."""
    secure_dir(CONFIG_DIR, 0o700)
    secure_dir(DROP_DIR, 0o755)
    paths = (CONFIG, ENV, UNIT, DROP)
    before = {path: read_managed(path, marker=path in (UNIT, DROP)) for path in paths}
    if (before[CONFIG] is None) != (before[ENV] is None):
        raise ProxyError("已有代理配置不完整，请先人工检查。")
    old_bot_active = active(SERVICE)
    old_proxy_active = active(PROXY_SERVICE)
    old_proxy_enabled = enabled(PROXY_SERVICE)
    if before[UNIT] is None and (old_proxy_active or old_proxy_enabled):
        raise ProxyError("发现同名代理服务，不能覆盖。")
    changed = False
    try:
        changed = True
        if old_bot_active:
            run("systemctl", "stop", SERVICE)
        if old_proxy_active:
            run("systemctl", "stop", PROXY_SERVICE)
        atomic_write(CONFIG, json.dumps(config, ensure_ascii=False).encode(), 0o600)
        atomic_write(ENV, env_text(port), 0o600)
        atomic_write(UNIT, unit_text(), 0o644)
        atomic_write(DROP, drop_text(), 0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", PROXY_SERVICE)
        if not active(PROXY_SERVICE):
            raise ProxyError("新节点代理没有正常启动。")
        wait_listener(port)
        if not active(PROXY_SERVICE):
            raise ProxyError("新节点代理已退出。")
        check_connectivity(port)
        if not active(PROXY_SERVICE):
            raise ProxyError("新节点代理已退出。")
        if old_bot_active:
            run("systemctl", "start", SERVICE)
            for _ in range(120):
                if active(SERVICE) and ready():
                    break
                time.sleep(0.5)
            else:
                raise ProxyError("机器人未通过就绪检查。")
            if not service_proxy_matches(port):
                raise ProxyError("机器人未使用新代理，可能有其他服务配置覆盖了代理设置。")
    except BaseException:
        failures = []

        def attempt(action) -> None:
            try:
                action()
            except Exception:
                failures.append(True)

        if changed:
            if old_bot_active:
                attempt(lambda: run("systemctl", "stop", SERVICE))
            attempt(lambda: run("systemctl", "stop", PROXY_SERVICE, check=False))
            # Remove links while the new unit still exists; after deleting it,
            # systemctl may be unable to disable an otherwise dangling link.
            if not old_proxy_enabled and UNIT.exists():
                attempt(lambda: run("systemctl", "disable", PROXY_SERVICE))
            for path, value in before.items():
                if value is None:
                    attempt(lambda p=path: p.unlink(missing_ok=True))
                else:
                    attempt(lambda p=path, v=value: atomic_write(p, v, 0o600 if p in (CONFIG, ENV) else 0o644))
            attempt(lambda: run("systemctl", "daemon-reload"))
            if old_proxy_enabled:
                attempt(lambda: run("systemctl", "enable", PROXY_SERVICE))
            if old_proxy_active:
                attempt(lambda: run("systemctl", "start", PROXY_SERVICE))
        if old_bot_active:
            attempt(lambda: run("systemctl", "start", SERVICE))
        if failures:
            print("回退有操作失败，请检查两个服务状态和日志。", file=sys.stderr)
        else:
            print("已尝试恢复原配置和服务，请检查服务状态。", file=sys.stderr)
        raise


def main() -> int:
    if os.geteuid() != 0:
        raise ProxyError("请以 root 运行安装器。")
    secure_dir(CONFIG_DIR, 0o700)
    def interrupted(_signum, _frame):
        raise ProxyError("操作中断。")
    signal.signal(signal.SIGTERM, interrupted)
    print("节点链接包含密码，请只粘贴在此终端，输入默认隐藏。")
    print("支持常见 VLESS、VMess、AnyTLS、Trojan、Hysteria2 和 Shadowsocks 链接。")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        link = getpass.getpass("节点链接（留空取消）: ")
    if not link:
        print("已取消节点设置。")
        return 2
    node = parse_node(link.strip())
    del link
    install_engine()
    port = ephemeral_port()
    config = build_config(node, port)
    print("正在验证节点与 Google、Telegram 的连接……")
    probe(config, port)
    apply(config, port)
    print("节点已保存并启用。网络连通不代表 Google 账号已获得使用资格。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (NodeError, ProxyError) as exc:
        print(f"代理配置未完成：{exc}", file=sys.stderr)
        sys.exit(1)
    except (OSError, subprocess.SubprocessError, tarfile.TarError,
            getpass.GetPassWarning, EOFError, KeyError, KeyboardInterrupt):
        # Never print exception details: upstream tools can include node secrets.
        print("代理配置未完成；请检查节点、网络或服务状态，原设置已尽力恢复。", file=sys.stderr)
        sys.exit(1)
