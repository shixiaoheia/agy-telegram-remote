#!/usr/bin/env python3
"""Private-chat Telegram bridge. One workspace, one job, no automatic task rerun."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agy_runner import Result, Runner
from settings import ConfigError, Settings
from state_store import Store, atomic_json
from telegram_api import TelegramAPI, TelegramError, chunks_utf16

LOG = logging.getLogger("agy_remote")
LABELS = {
    "success": "agy 返回结果",
    "no_text": "缺少最终文字回复",
    "permission": "存在权限拒绝，需要核对",
    "invalid": "结果无法完整解析",
    "error": "执行异常",
    "timed_out": "执行超时",
    "cancelled": "任务已取消",
    "interrupted": "上次任务被中断",
    "output_limit": "输出超过上限",
    "cleanup_failed": "进程清理未确认完成",
    "not_started": "任务没有启动",
    "running": "任务执行中",
}
CATEGORY_HELP = {
    "auth": "诊断信息疑似要求登录，请在服务器重新授权。",
    "quota": "诊断信息疑似涉及配额或限流，请检查账户额度。",
    "permission": "请检查 AGY_SKIP_PERMISSIONS 或 agy 的权限规则。",
    "network": "诊断信息疑似网络问题，请检查服务器连接。",
}


@dataclass
class Job:
    user: int
    chat: int
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None


def describe(record: dict) -> str:
    outcome = str(record.get("outcome", "error"))
    title = LABELS.get(outcome, "结果需核对")
    parts = [f"{title}｜任务 {record.get('job_id', '-') }"]
    if record.get("detail"):
        parts.append(str(record["detail"]))
    help_text = CATEGORY_HELP.get(record.get("category"))
    if help_text:
        parts.append(help_text)
    if record.get("text"):
        parts.append(str(record["text"]))
    if outcome not in {"success", "not_started", "running"}:
        parts.append("没有自动重跑任务。请先核对工作目录；/last 只取回记录，不重新执行。")
    return "\n\n".join(parts)


class Bridge:
    def __init__(self, settings: Settings, api: TelegramAPI, store: Store,
                 runner: Runner, ready_file: Path | None = None):
        self.settings, self.api, self.store, self.runner = settings, api, store, runner
        self.ready_file = ready_file
        self.polling_ready = False
        self._started_at = time.time()
        self.slot: Job | None = None
        self.stop = asyncio.Event()
        self.offset = store.offset()
        self._last_maintenance = 0.0

    async def send_text(self, chat: int, text: str) -> bool:
        try:
            for chunk in chunks_utf16(self.store.redact(text)):
                await self.api.send(chat, chunk)
            return True
        except TelegramError as error:
            LOG.warning("telegram_delivery_failed code=%s", error.code)
            return False

    async def _work(self, job: Job, prompt: str) -> None:
        # A single worker owns the slot until the execution, cleanup and storage finish.
        try:
            result = await self.runner.run(prompt, job.cancel)
            record = self.store.save(
                job.user, result.to_dict() | {"job_id": job.job_id, "delivery": "pending"}
            )
            LOG.info("job_finished id=%s outcome=%s", job.job_id, result.outcome)
            delivered = await self.send_text(job.chat, describe(record))
            self.store.save(job.user, record | {
                "delivery": "sent" if delivered else "failed_or_partial"
            })
        except asyncio.CancelledError:
            job.cancel.set()
            # Runner normally consumes cancellation only after cleaning its group.
            LOG.warning("worker_cancelled id=%s", job.job_id)
            raise
        except Exception as error:
            # Never print raw exception text: it may contain request URLs or secrets.
            LOG.error("worker_failed id=%s type=%s", job.job_id, type(error).__name__)
            self.runner.blocked = True
            await self.send_text(
                job.chat,
                f"任务 {job.job_id} 的结果保存或内部处理异常。"
                "已暂停新任务；请检查服务器。不要直接重复原任务。",
            )
        finally:
            if self.slot is job:
                self.slot = None

    async def handle(self, update: dict) -> None:
        if self.stop.is_set():
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        if chat.get("type") != "private":
            return
        user, chat_id = sender.get("id"), chat.get("id")
        if type(user) is not int or type(chat_id) is not int or user <= 0 or chat_id <= 0:
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        text = text.strip()
        command = text.split()[0].split("@")[0] if text.startswith("/") else ""
        if command == "/id":
            await self.send_text(chat_id, f"你的 Telegram 数字 ID：{user}")
            return
        if user not in self.settings.allowed or sender.get("is_bot") is True:
            return
        if command in {"/start", "/help"}:
            await self.send_text(
                chat_id, "直接发送任务给 agy。\n/status 查看状态\n/cancel 请求取消"
                "\n/last 取回本人最近结果（不会重新执行）\n/id 查看数字 ID"
                "\n每条普通消息独立执行，不保留对话上下文。",
            )
            return
        if command == "/status":
            if self.slot and self.slot.user == user:
                text = f"任务 {self.slot.job_id}：" + (
                    "正在取消并清理。" if self.slot.cancel.is_set() else "运行或回传中。"
                )
            elif self.runner.blocked:
                text = "已暂停新任务：进程清理或结果保存发生异常，请检查并重启服务。"
            else:
                text = "你当前没有任务。" + ("工作目录正被其他任务占用。" if self.slot else "")
            await self.send_text(chat_id, text)
            return
        if command == "/cancel":
            job = self.slot
            if job is not None and job.user == user:
                job.cancel.set()
                await self.send_text(chat_id, "已请求取消。会清理任务进程；已经发生的修改不会自动撤销。")
            else:
                await self.send_text(chat_id, "你当前没有可取消的任务。")
            return
        if command == "/last":
            try:
                record = self.store.load(user)
                text = describe(record) if record else "没有可取回的结果，或结果已过保留期。"
            except (OSError, ValueError):
                text = "无法读取最近结果，请检查服务器状态。"
            await self.send_text(chat_id, text)
            return
        if command:
            await self.send_text(chat_id, "不支持这个控制命令。发送 /help 查看用法。")
            return
        if len(text) > self.settings.max_prompt or "\x00" in text:
            await self.send_text(chat_id, f"任务过长或含非法字符，最多 {self.settings.max_prompt} 个字符。")
            return
        if self.runner.blocked:
            await self.send_text(chat_id, "新任务已暂停，请检查服务器并重启服务。")
            return
        if self.slot is not None:
            await self.send_text(chat_id, "工作目录已有任务，请等待完成，或由任务发起者发送 /cancel。")
            return

        job = Job(user, chat_id)
        self.slot = job  # reserve before first await
        try:
            self.store.maintain()
            self.store.save(user, {
                "job_id": job.job_id, "outcome": "running", "delivery": "pending",
                "detail": "任务准备或执行中；服务中断时不会自动重试。",
            })
            accepted = await self.send_text(chat_id, f"任务 {job.job_id} 已接收，准备调用 agy。")
            if not accepted or self.stop.is_set():
                self.store.save(user, {
                    "job_id": job.job_id, "outcome": "not_started",
                    "detail": "确认消息未成功投递或服务正在停止；没有启动 agy。",
                    "delivery": "failed",
                })
                self.slot = None
                return
            job.worker = asyncio.create_task(self._work(job, text))
        except Exception:
            self.slot = None
            self.runner.blocked = True
            LOG.error("job_prepare_failed id=%s", job.job_id)
            await self.send_text(chat_id, "任务准备失败，未启动 agy。请检查服务器存储和权限。")

    async def consume_updates(self, updates: object) -> None:
        if not isinstance(updates, list):
            raise TelegramError()
        for update in updates:
            if self.stop.is_set():
                break
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                raise TelegramError()
            uid = update["update_id"]
            if uid < self.offset:
                continue
            # Persist before dispatch: fail closed rather than replay side effects.
            # A crash here can drop this update; this is not exactly-once delivery.
            self.store.save_offset(uid + 1)
            self.offset = uid + 1
            await self.handle(update)

    async def initialize(self) -> None:
        me = await self.api.call("getMe")
        if not isinstance(me, dict) or me.get("is_bot") is not True:
            raise TelegramError()
        webhook = await self.api.call("getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise RuntimeError("An active webhook must be removed explicitly before polling")
        # Drop pre-start backlog. Negative offset is documented by Telegram.
        updates = await self.api.call(
            "getUpdates", offset=-1, limit=1, timeout=0, allowed_updates=["message"]
        )
        if not isinstance(updates, list):
            raise TelegramError()
        # Telegram may reseed update IDs after a long idle period. On startup
        # the explicit backlog drop defines a new watermark, not the old file.
        self.offset = 0
        for item in updates:
            if not isinstance(item, dict) or type(item.get("update_id")) is not int:
                raise TelegramError()
            self.offset = max(self.offset, item["update_id"] + 1)
        self.store.save_offset(self.offset)
        self.store.recover_interrupted()
        if self.ready_file is not None:
            atomic_json(self.ready_file, {
                "pid": os.getpid(),
                "initialized": True,
                "polling_ready": False,
                "started_at": self._started_at,
            })
        LOG.info("INITIALIZED pid=%s", os.getpid())

    async def run(self) -> None:
        try:
            await self.initialize()
            backoff = 1.0
            while not self.stop.is_set():
                if time.monotonic() - self._last_maintenance > 3600:
                    self.store.maintain()
                    self._last_maintenance = time.monotonic()
                try:
                    updates = await self.api.call(
                        "getUpdates", offset=self.offset, limit=25, timeout=10,
                        allowed_updates=["message"],
                    )
                    if not isinstance(updates, list):
                        raise TelegramError()
                    if not self.polling_ready:
                        self.polling_ready = True
                        if self.ready_file is not None:
                            atomic_json(self.ready_file, {
                                "pid": os.getpid(),
                                "initialized": True,
                                "polling_ready": True,
                                "started_at": self._started_at,
                                "ready_at": time.time(),
                            })
                        LOG.info("POLLING_READY pid=%s", os.getpid())
                    await self.consume_updates(updates)
                    backoff = 1.0
                except TelegramError as error:
                    if error.code in {401, 403, 409}:
                        raise
                    LOG.warning("telegram_poll_retry code=%s", error.code)
                    delay = max(backoff, float(error.retry_after))
                    try:
                        await asyncio.wait_for(self.stop.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(30.0, backoff * 2)
        finally:
            self.stop.set()
            if self.ready_file is not None:
                self.ready_file.unlink(missing_ok=True)
            job = self.slot
            if job is not None:
                job.cancel.set()
                if job.worker is not None:
                    await job.worker


async def start(settings: Settings, ready_file: Path | None) -> None:
    if os.geteuid() == 0:
        raise ConfigError("Bot 和 agy 禁止以 root 运行。")
    if not settings.workspace.is_dir() or not os.access(settings.workspace, os.R_OK | os.W_OK | os.X_OK):
        raise ConfigError("工作目录不可用。")
    store = Store(settings.state_dir, settings.allowed, settings.max_reply,
                  settings.retention_days, settings.token)
    store.lock()
    bridge = Bridge(settings, TelegramAPI(settings.token), store, Runner(settings), ready_file)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bridge.stop.set)
    try:
        await bridge.run()
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/agy-telegram-remote/config.env"))
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = Settings.load(args.config)
        asyncio.run(start(settings, args.ready_file))
        return 0
    except (ConfigError, PermissionError, BlockingIOError):
        LOG.error("startup_failed: configuration, permissions, or another local instance")
        return 78
    except TelegramError as error:
        LOG.error("startup_or_poll_failed: Telegram code=%s", error.code)
        return 78 if error.code in {401, 403, 409} else 1
    except (OSError, ValueError, RuntimeError):
        LOG.error("startup_failed: local state, webhook, or runtime error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
