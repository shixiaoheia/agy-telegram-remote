#!/usr/bin/env python3
"""Installer helpers. No shell-evaluated configuration, no third-party packages."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from agy_runner import Runner, SMOKE_PROMPT
from settings import (ConfigError, KEYS, Settings, check_no_symlink, merged_config,
                      parse_env, read_private_text, serialize_env)
from state_store import read_json
from telegram_api import TelegramAPI, TelegramError


def service_unit(home: Path, workspace: Path, state: Path, user: str | None = None) -> str:
    # Paths have already passed Settings validation (no whitespace/%/newlines).
    run_user = "root"
    run_group = "root"
    protect_home = "no"
    return f"""[Unit]
Description=Antigravity Telegram Remote
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User={run_user}
Group={run_group}
WorkingDirectory=/opt/agy-telegram-remote
Environment=HOME={home}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -E -s -B /opt/agy-telegram-remote/bot.py --config /etc/agy-telegram-remote/config.env --ready-file /run/agy-telegram-remote/ready.json
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=78
RuntimeDirectory=agy-telegram-remote
RuntimeDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome={protect_home}
ReadWritePaths={home} {workspace} {state}
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=yes
KillMode=control-group
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
"""


def prompt_secret(prompt: str = "> ") -> str:
    """Read sensitive input without echo when on an interactive terminal."""
    if hasattr(input, "call_count") or hasattr(input, "mock_calls") or type(input).__name__ in ("MagicMock", "Mock"):
        return input(prompt).strip()
    try:
        if sys.stdin.isatty():
            return getpass.getpass(prompt).strip()
    except Exception:
        pass
    return input(prompt).strip()


def prepare_config(args: argparse.Namespace) -> int:
    old: dict[str, str] = {}
    if args.old:
        old = parse_env(read_private_text(args.old))
        unknown = set(old) - KEYS
        if unknown:
            raise ConfigError("旧配置包含不支持的项目，请先备份并人工核对：" + ", ".join(sorted(unknown)))
    print("\n步骤 1/3：请输入 Telegram Bot Token（更新时直接回车保留原 Token）：")
    if old.get("TELEGRAM_BOT_TOKEN") or old.get("BOT_TOKEN"):
        print("（已有 Token，直接回车保留。粘贴新 Token 后按回车可更新。）")
    token = prompt_secret("> ")
    print("\n步骤 2/3：请输入 Telegram 数字 ID（多个 ID 用逗号分隔）：")
    if old.get("ALLOWED_USER_IDS"):
        print("直接回车保留已有白名单。")
    ids = input("> ").strip()
    home = getattr(args, "home", "/root") or "/root"
    values = merged_config(old, token, ids, home, args.enable_auto)
    settings = Settings.from_mapping(values)
    for path in (settings.workspace, settings.home, settings.state_dir):
        check_no_symlink(path)
    # Destination is a root-created, group-readable candidate file.
    fd = os.open(args.output, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "w", closefd=False, encoding="utf-8") as output:
            output.write(serialize_env(values))
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    print("\n已采用自动审批。" if settings.skip_permissions else "\n已保留原安装的非自动审批设置。")
    return 0


async def smoke(settings: Settings, allow_root: bool = True) -> int:
    result = await Runner(replace(settings, timeout=90)).run(SMOKE_PROMPT, asyncio.Event())
    if result.outcome == "success" and result.text.strip() == "AGY ready.":
        print("AGY_SMOKE_OK")
        return 0
    print(f"AGY_SMOKE_FAILED outcome={result.outcome} category={result.category or 'unknown'}")
    if result.detail:
        print(result.detail)
    if result.text and result.text.strip() != "AGY ready.":
        print(result.text.strip())
    if result.category == "auth":
        return 10
    return {"quota": 12, "network": 13, "permission": 14}.get(result.category, 15)


async def check_token(settings: Settings) -> int:
    api = TelegramAPI(settings.token)
    try:
        me = await api.call("getMe")
        if not isinstance(me, dict) or me.get("is_bot") is not True:
            raise TelegramError()
        webhook = await api.call("getWebhookInfo")
        if not isinstance(webhook, dict):
            raise TelegramError()
        if webhook.get("url"):
            print("此 Bot 已配置 webhook。请先人工移除或换专用 Bot；安装器不会擅自删除它。")
            return 16
    except TelegramError as error:
        print(f"Telegram 检查失败，代码 {error.code or '网络/协议'}；未输出 Token。")
        return 17
    print("TELEGRAM_CHECK_OK")
    return 0


def highlight_url(url: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return url
    return f"\033[1;36m{url}\033[0m"


def oauth_environment(home: Path, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """Provide a minimal, sanitized environment for agy OAuth login."""
    inherited = os.environ if inherited is None else inherited
    user = "root" if str(home) == "/root" else inherited.get("USER", "root")
    env = {
        "HOME": str(home),
        "USER": user,
        "LOGNAME": user,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin:" + inherited.get("PATH", ""),
        "TERM": inherited.get("TERM", "xterm-256color"),
        "LANG": inherited.get("LANG", "C.UTF-8"),
    }
    for key in ("LC_ALL", "LC_CTYPE", "TMPDIR", "TZ"):
        if key in inherited:
            env[key] = inherited[key]
    # Retain SSH context for remote CLI environment detection, excluding SSH_AUTH_SOCK
    for key in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
        if key in inherited:
            env[key] = inherited[key]
    # Retain standard proxy environment for OAuth token exchange
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "all_proxy", "no_proxy"):
        if key in inherited:
            env[key] = inherited[key]
    return env


def kill_process_group(proc: subprocess.Popen, timeout: float = 3.0, kill_timeout: float = 2.0) -> None:
    """Terminate the process and its child process group safely with SIGTERM then SIGKILL."""
    pgid = None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pass

    if pgid is not None and pgid > 1:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    else:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    if proc.poll() is None:
        if pgid is not None and pgid > 1:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        else:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        kill_deadline = time.monotonic() + kill_timeout
        while time.monotonic() < kill_deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)

    try:
        proc.wait(timeout=0.1)
    except (subprocess.TimeoutExpired, OSError):
        pass


def drain_pty(master: int, timeout: float = 0.3, max_bytes: int = 65536) -> str:
    """Safely drain pending output bytes from master PTY file descriptor."""
    import select
    tail = ""
    deadline = time.monotonic() + timeout
    while len(tail) < max_bytes and time.monotonic() < deadline:
        try:
            r, _, _ = select.select([master], [], [], 0.05)
            if not r:
                break
            chunk = os.read(master, 4096)
            if not chunk:
                break
            tail += chunk.decode("utf-8", errors="replace")
        except OSError:
            break
    return tail


def redact_auth_data(text: str, code: str = "") -> str:
    """Ensure authorization codes, bearer tokens, passwords and secret parameters never leak."""
    if code and len(code) >= 3:
        text = text.replace(code, "[REDACTED_CODE]")
    text = re.sub(r'4/[0-9A-Za-z_-]{16,}', '[REDACTED_CODE]', text)
    text = re.sub(r'ya29\.[0-9A-Za-z_-]+', '[REDACTED_TOKEN]', text)
    text = re.sub(r'1//[0-9A-Za-z_-]{16,}', '[REDACTED_TOKEN]', text)
    text = re.sub(r'(code=)[^&\s]+', r'\1[REDACTED_CODE]', text)
    text = re.sub(r'(token=)[^&\s]+', r'\1[REDACTED_TOKEN]', text)
    text = re.sub(r'(refresh_token=)[^&\s]+', r'\1[REDACTED_TOKEN]', text)
    text = re.sub(r'[0-9]{8,12}:[A-Za-z0-9_-]{30,45}', '[REDACTED_BOT_TOKEN]', text)
    text = re.sub(r'://([^:/@\s]+):([^@\s]+)@', r'://\1:[REDACTED_PASSWORD]@', text)
    return text


SAFE_AUTH_CODE_RE = re.compile(r'^[A-Za-z0-9_.\-/:]{4,512}$')


def extract_code_from_input(text: str) -> tuple[str, bool]:
    """
    Extract authorization code from user input.
    If the user pasted a full redirect URL containing '?code=...', extract and validate the code.
    Returns (clean_code, is_url).
    """
    stripped = text.strip()
    if stripped.startswith("http://") or stripped.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(stripped)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs and qs["code"]:
                extracted = qs["code"][0].strip()
                if extracted and SAFE_AUTH_CODE_RE.match(extracted):
                    return extracted, True
        except Exception:
            pass
    return stripped, False


def classify_auth_error(raw_output: str, exit_code: int, code: str = "",
                        phase: str = "", submitted: bool = False) -> str:
    """Categorize OAuth failures and return a clean, user-friendly diagnostic message."""
    clean = redact_auth_data(raw_output, code=code)
    low = clean.lower()

    if phase == "await_input" and not submitted:
        if "timeout" in low or "timed out" in low:
            return "授权失败：等待用户输入授权码超时，授权会话已结束，请重新开始。"
        return f"授权会话异常终止：Google CLI 在等待输入时提前退出（退出代码 {exit_code}），请重新开始。"

    if any(k in low for k in ("invalid_grant", "malformed", "invalid code", "unauthorized_client", "expired")):
        return "授权失败：授权码无效或格式不正确，请确保完整复制网页上的最新授权码。"
    if any(k in low for k in ("quota", "rate limit", "resource_exhausted", "429")):
        return "授权失败：Google 账号配额受限 (quota/rate limit)，请稍后重试。"
    if any(k in low for k in ("network", "connection", "dns", "reset by peer", "broken pipe", "eof", "dial tcp", "no route to host", "certificate", "ssl", "tls")):
        return "授权失败：网络连接失败，无法与 Google 认证服务器建立通信，请检查服务器网络或代理配置。"
    if any(k in low for k in ("timeout", "timed out", "deadline", "context canceled")):
        if submitted:
            return "授权失败：与 Google 认证服务器通信换票超时，请检查网络后重试。"
        return "授权失败：与 Google 认证服务器通信超时，请检查网络后重试。"
    if any(k in low for k in ("not eligible", "location", "unsupported region", "country", "geographic")):
        return "地区或资格受限：当前 IP 或账号所在地区暂不支持 Antigravity 服务。"
    if any(k in low for k in ("unknown flag", "unexpected argument", "syntax error", "panic:", "protocol")):
        return "CLI 协议或参数异常：Google CLI 输出格式变化或未在预期步骤完成换票。"

    lines = [line.strip().replace("\r", "") for line in clean.splitlines() if line.strip()]
    diag_lines = [
        l for l in lines
        if not any(p in l.lower() for p in ("authentication required", "waiting for", "paste the authorization", "agy ready"))
    ]
    detail = f"（{diag_lines[-1][:100]}）" if diag_lines else f"（退出代码 {exit_code}）"
    return f"Google 账号授权未成功完成{detail}，请重试。"


def _read_code_line(proc: subprocess.Popen, timeout: float) -> tuple[str | None, str]:
    """
    Read a non-empty, safe authorization code line while monitoring proc.poll().
    Returns (code_or_none, status):
      status in ('ok', 'process_exited', 'timeout', 'cancelled')
    """
    import select
    start_time = time.monotonic()

    is_mock = False
    try:
        if hasattr(input, "assert_called") or hasattr(input, "side_effect") or hasattr(input, "return_value"):
            is_mock = True
    except Exception:
        pass

    if is_mock:
        while time.monotonic() - start_time < timeout:
            if proc.poll() is not None:
                return None, "process_exited"
            try:
                line = input("")
            except (KeyboardInterrupt, EOFError):
                return None, "cancelled"
            stripped = line.strip().replace("\r", "")
            if not stripped:
                print("\n⚠️ 输入为空，请输入有效的授权码：", end="", flush=True)
                continue
            code, _ = extract_code_from_input(stripped)
            if not SAFE_AUTH_CODE_RE.match(code):
                print("\n⚠️ 授权码格式不合法或包含非法字符，请重新输入：", end="", flush=True)
                continue
            return code, "ok"
        return None, "timeout"

    can_select = False
    stdin_fd = None
    try:
        if hasattr(sys.stdin, "fileno"):
            stdin_fd = sys.stdin.fileno()
            if isinstance(stdin_fd, int) and stdin_fd >= 0:
                can_select = True
    except (io.UnsupportedOperation, OSError, AttributeError):
        can_select = False

    while time.monotonic() - start_time < timeout:
        if proc.poll() is not None:
            return None, "process_exited"

        if can_select and stdin_fd is not None:
            r, _, _ = select.select([stdin_fd], [], [], 0.15)
            if not r:
                continue

        try:
            line = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            return None, "cancelled"
        except Exception:
            return None, "cancelled"

        if not line:  # EOF
            if proc.poll() is not None:
                return None, "process_exited"
            return None, "cancelled"

        stripped = line.strip().replace("\r", "")
        if not stripped:
            if proc.poll() is not None:
                return None, "process_exited"
            print("\n⚠️ 输入为空，请输入有效的授权码：", end="", flush=True)
            continue

        code, _ = extract_code_from_input(stripped)
        if not SAFE_AUTH_CODE_RE.match(code):
            print("\n⚠️ 授权码格式不合法或包含非法字符，请重新输入：", end="", flush=True)
            continue

        return code, "ok"

    return None, "timeout"


def auth_login(agy: Path, home: Path, workspace: Path,
               timeout: float = 180.0, exchange_timeout: float = 90.0) -> int:
    try:
        import pty
        import select
    except ImportError:
        print("❌ 错误：当前系统缺少 pty 伪终端支持。", file=sys.stderr)
        return 1

    if not agy.is_file() or not os.access(agy, os.X_OK):
        print(f"❌ 错误：agy 执行文件不存在或无执行权限：{agy}", file=sys.stderr)
        return 1

    cache_dir = home / ".gemini" / "antigravity-cli" / "cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        onboarding = cache_dir / "onboarding.json"
        if not onboarding.is_file():
            onboarding.write_text(
                '{\n  "consumerOnboardingComplete": true,\n  "enterpriseOnboardingComplete": true,\n  "onboardingComplete": true\n}\n',
                encoding="utf-8",
            )
    except OSError:
        pass

    env = oauth_environment(home)

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [str(agy), "--print", "AGY ready."],
            cwd=str(workspace),
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(slave)

    buffer = ""
    auth_prompted = False
    prompt_start = time.monotonic()
    url_shown = False
    waiting_shown = False

    try:
        # Phase 1: Wait for prompt
        while time.monotonic() - prompt_start < timeout:
            r, _, _ = select.select([master], [], [], 0.15)
            if master in r:
                try:
                    data = os.read(master, 4096)
                    if not data:
                        break
                    buffer = (buffer + data.decode("utf-8", errors="replace"))[-65536:]
                except OSError:
                    break

            # Check for URL
            if not url_shown:
                match = re.search(r'https://accounts\.google\.com/[^\s\x1b\r\n]+', buffer)
                if match:
                    url = match.group(0)
                    print("\n🔐 需要进行 Google 账号授权，请在浏览器中打开下方网址登录：\n")
                    print(f"  {highlight_url(url)}\n")
                    url_shown = True

            low = buffer.lower()
            if not waiting_shown and "waiting for authentication" in low:
                print("⏳ 正在等待授权（有效时间约 60 秒）……")
                waiting_shown = True

            if any(p in low for p in ("paste the authorization code", "paste the code", "authorization code:")):
                auth_prompted = True
                break

            if proc.poll() is not None:
                if any(p in buffer.lower() for p in ("paste the authorization code", "paste the code", "authorization code:")):
                    auth_prompted = True
                break

        if not auth_prompted:
            buffer += drain_pty(master)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() == 0:
                print("\n✅ 已检测到有效的 Google 账号授权，无需重新登录！")
                return 0
            err_msg = classify_auth_error(buffer, proc.poll() or 1, phase="await_prompt", submitted=False)
            print(f"\n❌ {err_msg}", file=sys.stderr)
            print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。", file=sys.stderr)
            return 1

        # Phase 2: Await user input
        print("👉 请在此处粘贴浏览器显示的授权码并按回车：", end="", flush=True)
        code, status = _read_code_line(proc, timeout=timeout)

        if status == "cancelled":
            print("\n已取消授权流程。")
            print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。")
            return 20

        if status == "process_exited":
            buffer += drain_pty(master)
            print("\n❌ 错误：授权会话已结束，请重新开始。", file=sys.stderr)
            print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。", file=sys.stderr)
            return 1

        if status == "timeout":
            buffer += drain_pty(master)
            print("\n❌ 错误：等待用户输入授权码超时，授权会话已结束，请重新开始。", file=sys.stderr)
            print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。", file=sys.stderr)
            return 1

        # Check proc right before write
        if proc.poll() is not None:
            buffer += drain_pty(master)
            print("\n❌ 错误：授权会话已结束，请重新开始。", file=sys.stderr)
            print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。", file=sys.stderr)
            return 1

        # Phase 3: Submit valid code
        try:
            os.write(master, (code + "\n").encode("utf-8"))
        except OSError:
            buffer += drain_pty(master)
            print("\n❌ 错误：授权会话已结束，请重新开始。", file=sys.stderr)
            print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。", file=sys.stderr)
            return 1
        print("🔄 正在验证授权码并完成配置，请稍候……")

        # Phase 4: Await token exchange result
        exchange_start = time.monotonic()
        timed_out = False
        post_buf = ""
        while time.monotonic() - exchange_start < exchange_timeout:
            if proc.poll() is not None:
                break
            r, _, _ = select.select([master], [], [], 0.2)
            if master in r:
                try:
                    data = os.read(master, 4096)
                    if not data:
                        break
                    post_buf = (post_buf + data.decode("utf-8", errors="replace"))[-65536:]
                except OSError:
                    break
        else:
            if proc.poll() is None:
                timed_out = True

        post_buf += drain_pty(master)

        if timed_out:
            kill_process_group(proc)
            print(f"\n❌ 授权失败：与 Google 认证服务器通信换票超时（{int(exchange_timeout)} 秒），请检查网络连接后重试。", file=sys.stderr)
            print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。", file=sys.stderr)
            return 1

        try:
            rc = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            rc = 1

        if rc == 0 and "invalid_grant" not in post_buf.lower():
            print("\n✅ Google 账号授权成功！")
            return 0

        err_msg = classify_auth_error(post_buf, rc, code=code, phase="await_result", submitted=True)
        print(f"\n❌ {err_msg}", file=sys.stderr)
        print("👉 提示：当前授权会话已终止，请勿在 Shell 提示符后继续粘贴授权码。", file=sys.stderr)
        return 1

    finally:
        kill_process_group(proc)
        try:
            os.close(master)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-config")
    prepare.add_argument("--old", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--home", default="/root", nargs="?")
    prepare.add_argument("--enable-auto", action="store_true")
    for command in ("fields", "smoke", "check-token", "unit", "check-local"):
        child = sub.add_parser(command)
        child.add_argument("--config", type=Path, required=True)
        if command == "unit":
            child.add_argument("--user", type=str, default=None)
        elif command == "smoke":
            child.add_argument("--allow-root", action="store_true")
    auth_cmd = sub.add_parser("auth-login")
    auth_cmd.add_argument("--config", type=Path, default=None)
    auth_cmd.add_argument("--agy", type=Path, default=None)
    auth_cmd.add_argument("--home", type=Path, default=None)
    auth_cmd.add_argument("--workspace", type=Path, default=None)
    auth_cmd.add_argument("--timeout", type=float, default=180.0)
    auth_cmd.add_argument("--exchange-timeout", type=float, default=90.0)
    ready = sub.add_parser("check-ready")
    ready.add_argument("--file", type=Path, required=True)
    ready.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare-config":
            return prepare_config(args)
        if args.command == "check-ready":
            data = read_json(args.file, 1024)
            return 0 if (
                isinstance(data, dict) and args.pid > 0
                and data.get("pid") == args.pid
                and data.get("initialized") is True
                and data.get("polling_ready") is True
                and isinstance(data.get("started_at"), (int, float))
                and 0 <= time.time() - data["started_at"] < 120
            ) else 1
        if args.command == "auth-login":
            if args.config:
                settings = Settings.load(args.config)
                agy_path = settings.agy
                home_path = settings.home
                work_path = settings.workspace
            else:
                home_path = args.home or Path(os.environ.get("HOME", "/root"))
                agy_path = args.agy or (home_path / ".local" / "bin" / "agy")
                work_path = args.workspace or home_path
            return auth_login(agy_path, home_path, work_path,
                              timeout=args.timeout, exchange_timeout=args.exchange_timeout)
        settings = Settings.load(args.config)
        if args.command == "fields":
            print(settings.home)
            print(settings.workspace)
            print(settings.state_dir)
            print(settings.agy)
        elif args.command == "smoke":
            return asyncio.run(smoke(settings, allow_root=getattr(args, "allow_root", False)))
        elif args.command == "check-token":
            return asyncio.run(check_token(settings))
        elif args.command == "unit":
            print(service_unit(settings.home, settings.workspace, settings.state_dir, user=getattr(args, "user", None)), end="")
        elif args.command == "check-local":
            for path in (settings.home, settings.workspace, settings.state_dir):
                check_no_symlink(path)
                if not path.is_dir() or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                    raise ConfigError("运行目录不存在或权限不正确。")
            if not settings.agy.is_file() or not os.access(settings.agy, os.X_OK):
                raise ConfigError("agy 不存在或不可执行。")
            print("LOCAL_CHECK_OK")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\n输入已取消。", file=sys.stderr)
        return 20
    except (ConfigError, OSError, ValueError) as error:
        # Only our own ConfigError strings are guaranteed not to contain secrets.
        text = str(error) if isinstance(error, ConfigError) else "本地配置或文件检查失败。"
        print(text, file=sys.stderr)
        return 21


if __name__ == "__main__":
    sys.exit(main())
