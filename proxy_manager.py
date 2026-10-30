#!/usr/bin/env python3
"""Manage the project's private, loopback-only sing-box proxy.

Node secrets are read with terminal echo disabled and never passed on a command
line. The surrounding installer holds its deployment lock before invoking us.
"""
from __future__ import annotations

import contextlib
import base64
import getpass
import hashlib
import json
import os
import re
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
PROXY_RECOVERY_VERSION = 1
ARCHES = {
    "x86_64": ("amd64", "699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968"),
    "aarch64": ("arm64", "436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26"),
}
CONFIG_DIR = Path("/etc/agy-telegram-remote")
ENGINE_DIR = Path("/opt/agy-telegram-remote-proxy")
CONFIG = CONFIG_DIR / "proxy.json"
ENV = CONFIG_DIR / "proxy.env"
JOURNAL = CONFIG_DIR / ".proxy-transaction.json"
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
        limit = 8 * 1024 * 1024 if path == JOURNAL else 1024 * 1024
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & (0o077 if path in (CONFIG, ENV, JOURNAL) else 0o022)
                or info.st_size > limit):
            raise ProxyError(f"文件不安全：{path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
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
        sync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def sync_directory(path: Path) -> None:
    # A file fsync alone does not make the rename durable across power loss.
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def snapshot_paths() -> tuple[Path, ...]:
    return CONFIG, ENV, UNIT, DROP


def save_transaction(before: dict[Path, bytes | None], bot: bool, proxy: bool, enabled_: bool) -> None:
    if read_managed(JOURNAL) is not None:
        raise ProxyError("存在未恢复的代理切换记录，请先恢复。")
    encoded = [None if before[path] is None else base64.b64encode(before[path]).decode("ascii")
               for path in snapshot_paths()]
    record = {"version": 1, "files": encoded, "bot_active": bot,
              "proxy_active": proxy, "proxy_enabled": enabled_}
    atomic_write(JOURNAL, json.dumps(record, separators=(",", ":")).encode(), 0o600)


def read_transaction() -> tuple[dict[Path, bytes | None], bool, bool, bool] | None:
    raw = read_managed(JOURNAL)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"version", "files", "bot_active", "proxy_active", "proxy_enabled"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["files"], list) or len(value["files"]) != 4
                or any(type(value[key]) is not bool for key in ("bot_active", "proxy_active", "proxy_enabled"))):
            raise ValueError
        decoded = [None if item is None else base64.b64decode(item, validate=True) for item in value["files"]]
        if any(item is not None and len(item) > 1024 * 1024 for item in decoded):
            raise ValueError
        for item in decoded[2:]:
            if item is not None and not item.startswith(MARKER.encode()):
                raise ValueError
        before = dict(zip(snapshot_paths(), decoded))
        return before, value["bot_active"], value["proxy_active"], value["proxy_enabled"]
    except (TypeError, ValueError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyError("代理恢复记录无效，已停止；请人工检查。") from exc


def clear_transaction() -> None:
    read_managed(JOURNAL)
    JOURNAL.unlink(missing_ok=True)
    sync_directory(JOURNAL.parent)


def previous_port(before: dict[Path, bytes | None]) -> int | None:
    if before[CONFIG] is None and before[ENV] is None:
        return None
    if before[CONFIG] is None or before[ENV] is None:
        raise ProxyError("已有代理配置不完整，请先人工检查。")
    try:
        match = re.match(rb"HTTP_PROXY=http://127\.0\.0\.1:([0-9]{1,5})\n", before[ENV])
        if not match:
            raise ValueError
        port = int(match.group(1))
        if not 1 <= port <= 65535 or before[ENV] != env_text(port):
            raise ValueError
        cfg = json.loads(before[CONFIG])
        inbound = cfg["inbounds"]
        if (not isinstance(inbound, list) or len(inbound) != 1 or inbound[0].get("type") != "http"
                or inbound[0].get("listen") != "127.0.0.1" or inbound[0].get("listen_port") != port):
            raise ValueError
        return port
    except (TypeError, ValueError, KeyError, AttributeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyError("已有代理端口配置不一致，请先恢复或人工检查。") from exc


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
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(executable, ENGINE)
                sync_directory(ENGINE_DIR)
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
    for label, url in (("Google", "https://antigravity.google/"),
                       ("Telegram", "https://api.telegram.org/")):
        result = subprocess.run(
            ["curl", "--silent", "--show-error", "--output", "/dev/null",
             "--write-out", "%{http_code}", "--max-time", "20", "--noproxy", "",
             "--proxy", f"http://127.0.0.1:{port}", url],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=False, timeout=25,
        )
        if result.returncode or not re.fullmatch(r"[1-5][0-9]{2}", result.stdout):
            raise ProxyError(f"节点无法连接 {label}，请检查节点和网络。")
        if int(result.stdout) >= 400:
            print(f"{label} 已建立 HTTPS 连接，但返回 HTTP {result.stdout}；请检查服务、地区或网络。")
        else:
            print(f"{label} HTTPS 连接正常（HTTP {result.stdout}）。")


def check_telegram_identity(port: int) -> bool | None:
    """Check an existing bot's identity through the candidate without polling."""
    config = CONFIG_DIR / "config.env"
    if not config.exists() and not config.is_symlink():
        print("Telegram 身份验证：尚无机器人配置，安装时会继续验证。")
        return None
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
           if key in os.environ}
    address = f"http://127.0.0.1:{port}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[key] = address
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    env["ALL_PROXY"] = env["all_proxy"] = ""
    try:
        result = subprocess.run(
            [sys.executable, "-E", "-s", "-B", str(Path(__file__).resolve().with_name("manage.py")),
             "check-token", "--config", str(config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, check=False, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        print("Telegram 身份验证：暂未完成，请检查代理和网络。")
        return False
    if result.returncode == 0 and b"TELEGRAM_CHECK_OK" in result.stdout.splitlines():
        print("Telegram 身份验证：通过（不测试消息接收冲突）。")
        return True
    if result.returncode == 16:
        print("Telegram 身份验证：当前机器人配置了 webhook，请人工检查。")
    else:
        print("Telegram 身份验证：未通过，请检查 Token、代理和网络。")
    return False


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
        check_telegram_identity(port)
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
Type=exec
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
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
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
    return (MARKER + f"[Unit]\nRequires={PROXY_SERVICE}\nAfter={PROXY_SERVICE}\n"
            f"[Service]\nEnvironmentFile={ENV}\n").encode()


def check_bot_proxy_environment(environ: dict[str, str] | None = None) -> None:
    """Fail closed when a project proxy exists but the bot cannot use it."""
    before = {CONFIG: read_managed(CONFIG), ENV: read_managed(ENV)}
    port = previous_port(before)
    if port is None:
        if read_managed(JOURNAL) is not None:
            raise ProxyError("上次代理切换未完成；请先通过菜单 4 恢复。")
        return  # A direct or externally managed installation remains unchanged.
    environ = os.environ if environ is None else environ
    expected = dict(line.split("=", 1) for line in env_text(port).decode("ascii").splitlines())
    if any(environ.get(key) != value for key, value in expected.items()):
        raise ProxyError("代理配置存在但机器人没有加载代理环境；请检查服务配置。")
    try:
        wait_listener(port, timeout=10)
    except ProxyError as exc:
        raise ProxyError("代理配置存在但本机代理未就绪；请检查代理服务。") from exc


def ready() -> bool:
    result = subprocess.run(["systemctl", "show", SERVICE, "-p", "MainPID", "--value"],
                            capture_output=True, text=True, timeout=10, check=False)
    pid = result.stdout.strip()
    if not pid.isdecimal() or pid == "0":
        return False
    helper = Path(__file__).resolve().with_name("manage.py")
    return run("/usr/bin/python3", "-E", "-s", "-B", str(helper),
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


def wait_bot(port: int | None) -> None:
    for _ in range(120):
        if active(SERVICE) and ready() and (port is None or service_proxy_matches(port)):
            return
        time.sleep(0.5)
    raise ProxyError("机器人未通过启动和代理环境检查。")


def restore_transaction(before: dict[Path, bytes | None], old_bot_active: bool,
                        old_proxy_active: bool, old_proxy_enabled: bool) -> bool:
    failures = []

    def attempt(action) -> bool:
        try:
            action()
            return True
        except Exception:
            failures.append(True)
            return False

    # A previous interrupted switch may have restarted the bot after the record
    # was written. The caller has already warned before stopping an active bot.
    if old_bot_active or active(SERVICE):
        attempt(lambda: run("systemctl", "stop", SERVICE))
    attempt(lambda: run("systemctl", "stop", PROXY_SERVICE, check=False))
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
        if attempt(lambda: run("systemctl", "start", PROXY_SERVICE)):
            port = previous_port(before)
            if port is not None:
                attempt(lambda: wait_listener(port))
    if old_bot_active and not failures:
        if attempt(lambda: run("systemctl", "start", SERVICE)):
            attempt(lambda: wait_bot(previous_port(before)))
    if failures:
        print("回退有操作失败，恢复记录已保留；请检查两个服务状态和日志。", file=sys.stderr)
        return False
    print("已尝试恢复原配置和服务，请检查服务状态。", file=sys.stderr)
    return True


def recover_pending() -> bool:
    pending = read_transaction()
    if pending is None:
        return False
    print("检测到上次节点切换被中断，正在恢复之前的配置……")
    if not restore_transaction(*pending):
        raise ProxyError("未能完成代理恢复；记录已保留，请检查服务后重试。")
    clear_transaction()
    print("已恢复之前的代理配置；未自动重跑任何任务。")
    return True


def apply(config: dict, port: int) -> None:
    """Switch only after a candidate probe. Roll back both services on failure."""
    secure_dir(CONFIG_DIR, 0o700)
    secure_dir(DROP_DIR, 0o755)
    before = {path: read_managed(path, marker=path in (UNIT, DROP)) for path in snapshot_paths()}
    old_port = previous_port(before)
    if old_port is not None:
        # The same local port keeps the two independently persisted files
        # consistent even if power fails between their atomic replacements.
        port = old_port
        config = dict(config)
        config["inbounds"] = [dict(config["inbounds"][0], listen_port=port)]
    old_bot_active = active(SERVICE)
    old_proxy_active = active(PROXY_SERVICE)
    old_proxy_enabled = enabled(PROXY_SERVICE)
    if before[UNIT] is None and (old_proxy_active or old_proxy_enabled):
        raise ProxyError("发现同名代理服务，不能覆盖。")
    save_transaction(before, old_bot_active, old_proxy_active, old_proxy_enabled)
    try:
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
            wait_bot(port)
    except BaseException:
        if restore_transaction(before, old_bot_active, old_proxy_active, old_proxy_enabled):
            clear_transaction()
        raise
    clear_transaction()


def service_installed() -> bool:
    result = subprocess.run(["systemctl", "show", SERVICE, "-p", "LoadState", "--value"],
                            capture_output=True, text=True, timeout=10, check=False)
    return result.returncode == 0 and result.stdout.strip() == "loaded"


def start_saved_bot() -> None:
    before = {CONFIG: read_managed(CONFIG), ENV: read_managed(ENV)}
    port = previous_port(before)
    if port is None:
        raise ProxyError("节点配置不存在，无法启动机器人。")
    run("systemctl", "reset-failed", SERVICE, check=False)
    try:
        run("systemctl", "start", SERVICE)
        wait_bot(port)
        print("机器人已启动并通过就绪检查。")
        return
    except (subprocess.SubprocessError, ProxyError):
        pass
    raise ProxyError("节点已保存，但机器人未通过启动检查；请查看机器人服务日志。")


def main() -> int:
    if os.geteuid() != 0:
        raise ProxyError("请以 root 运行安装器。")
    secure_dir(CONFIG_DIR, 0o700)
    def interrupted(_signum, _frame):
        raise ProxyError("操作中断。")
    signal.signal(signal.SIGTERM, interrupted)
    recover_pending()
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
    print("节点已保存并启用。HTTPS 检查不验证 Google 账号资格或模型额度。")
    if service_installed() and not active(SERVICE):
        print("机器人服务目前未运行。")
        while True:
            try:
                answer = input("是否现在启动机器人？[y/n]： ").strip().lower()
            except EOFError:
                print("已保留停止状态；代理节点已保存。")
                break
            if answer == "n":
                print("已保留停止状态；代理节点已保存。")
                break
            if answer == "y":
                start_saved_bot()
                break
            print("请输入 y 或 n。")
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
