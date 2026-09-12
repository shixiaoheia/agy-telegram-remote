#!/usr/bin/env python3
"""Installer helpers. No shell-evaluated configuration, no third-party packages."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from agy_runner import Runner, SMOKE_PROMPT
from settings import (ConfigError, KEYS, Settings, check_no_symlink, merged_config,
                      parse_env, read_private_text, serialize_env)
from state_store import read_json
from telegram_api import TelegramAPI, TelegramError


def service_unit(home: Path, workspace: Path, state: Path) -> str:
    # Paths have already passed Settings validation (no whitespace/%/newlines).
    return f"""[Unit]
Description=Antigravity Telegram Remote
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=agy-tg
Group=agy-tg
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
ProtectHome=read-only
ReadWritePaths={home} {workspace} {state}
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=yes
KillMode=control-group
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
"""


def prepare_config(args: argparse.Namespace) -> int:
    old: dict[str, str] = {}
    if args.old:
        old = parse_env(read_private_text(args.old))
        unknown = set(old) - KEYS
        if unknown:
            raise ConfigError("旧配置包含不支持的项目，请先备份并人工核对：" + ", ".join(sorted(unknown)))
    print("\n步骤 1/3：请输入 Telegram Bot Token（输入隐藏；更新时直接回车保留）：")
    token = getpass.getpass("> ")
    print("\n步骤 2/3：请输入 Telegram 数字 ID（多个 ID 用逗号分隔）：")
    if old.get("ALLOWED_USER_IDS"):
        print("直接回车保留已有白名单。")
    ids = input("> ").strip()
    values = merged_config(old, token.strip(), ids, args.home, args.enable_auto)
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


async def smoke(settings: Settings) -> int:
    if os.geteuid() == 0:
        raise ConfigError("自检必须以 agy-tg 运行，不允许 root。")
    result = await Runner(replace(settings, timeout=90)).run(SMOKE_PROMPT, asyncio.Event())
    if result.outcome == "success" and result.text.strip() == "AGY ready.":
        print("AGY_SMOKE_OK")
        return 0
    print(f"AGY_SMOKE_FAILED outcome={result.outcome} category={result.category or 'unknown'}")
    print(result.detail or "未取得预期的测试回复。")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-config")
    prepare.add_argument("--old", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--home", required=True)
    prepare.add_argument("--enable-auto", action="store_true")
    for command in ("fields", "smoke", "check-token", "unit", "check-local"):
        child = sub.add_parser(command)
        child.add_argument("--config", type=Path, required=True)
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
        settings = Settings.load(args.config)
        if args.command == "fields":
            print(settings.home)
            print(settings.workspace)
            print(settings.state_dir)
            print(settings.agy)
        elif args.command == "smoke":
            return asyncio.run(smoke(settings))
        elif args.command == "check-token":
            return asyncio.run(check_token(settings))
        elif args.command == "unit":
            print(service_unit(settings.home, settings.workspace, settings.state_dir), end="")
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
