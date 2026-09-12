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
from settings import ConfigError, MODEL_RE, OFFICIAL_MODELS, resolve_model, Settings
from state_store import Store, atomic_json
from telegram_api import TelegramAPI, TelegramError, chunks_utf16

LOG = logging.getLogger("agy_remote")
LABELS = {
    "success": "✅ 任务完成",
    "no_text": "⚠️ 缺少最终文字回复",
    "permission": "🚫 存在权限拒绝，需要核对",
    "invalid": "⚠️ 结果无法完整解析",
    "error": "❌ 执行异常",
    "timed_out": "⏱️ 执行超时",
    "cancelled": "🛑 任务已取消",
    "interrupted": "⚠️ 上次任务被中断",
    "output_limit": "📏 输出超过上限",
    "cleanup_failed": "⚠️ 进程清理未确认完成",
    "not_started": "⏹️ 任务没有启动",
    "running": "⏳ 任务执行中",
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
    model: str = ""


def describe(record: dict) -> str:
    outcome = str(record.get("outcome", "error"))
    title = LABELS.get(outcome, "结果需核对")
    model_tag = f"｜{record['model']}" if record.get("model") else ""
    duration = record.get("duration_seconds")
    duration_tag = f"｜⏱️ {duration}s" if isinstance(duration, (int, float)) and duration > 0 else ""
    parts = [f"{title}｜任务 {record.get('job_id', '-') }{model_tag}{duration_tag}"]
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
        self.replies: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=32)
        self.reply_worker: asyncio.Task | None = None
        self._next_id_reply = 0.0

    async def send_text(self, chat: int, text: str) -> bool:
        try:
            chunks = chunks_utf16(self.store.redact(text))
            total = len(chunks)
            for i, chunk in enumerate(chunks):
                suffix = f"\n\n📄 [第 {i+1}/{total} 页]" if total > 1 else ""
                await self.api.send(chat, chunk + suffix)
            return True
        except TelegramError as error:
            LOG.warning("telegram_delivery_failed code=%s", error.code)
            return False

    def queue_reply(self, chat: int, text: str) -> None:
        # Control replies must never hold up update ingestion or cancellation.
        if self.stop.is_set():
            return
        try:
            self.replies.put_nowait((chat, text))
        except asyncio.QueueFull:
            return
        if self.reply_worker is None or self.reply_worker.done():
            self.reply_worker = asyncio.create_task(self._send_replies())

    async def _send_replies(self) -> None:
        while not self.replies.empty():
            chat, text = self.replies.get_nowait()
            try:
                await self.send_text(chat, text)
            finally:
                self.replies.task_done()

    async def _accept_and_work(self, job: Job, prompt: str) -> None:
        try:
            model_info = f"（模型：{job.model}）" if job.model else ""
            accepted = await self.send_text(job.chat, f"⏳ 任务 {job.job_id} 已接收{model_info}，准备调用 agy。")
            if not accepted or self.stop.is_set() or job.cancel.is_set():
                self.store.save(job.user, {
                    "job_id": job.job_id, "outcome": "not_started",
                    "detail": "确认消息未成功投递、任务已取消或服务正在停止；没有启动 agy。",
                    "delivery": "sent" if accepted else "failed",
                    "model": job.model,
                })
                return
            await self._work(job, prompt)
        except Exception as error:
            self.runner.blocked = True
            LOG.error("job_accept_failed id=%s type=%s", job.job_id, type(error).__name__)
            self.queue_reply(job.chat, "任务准备失败，未启动或未确认结果。请检查服务器，不要直接重跑。")
        finally:
            if self.slot is job:
                self.slot = None

    async def _work(self, job: Job, prompt: str) -> None:
        # A single worker owns the slot until the execution, cleanup and storage finish.
        try:
            try:
                result = await self.runner.run(prompt, job.cancel, model=job.model or None)
            except TypeError:
                result = await self.runner.run(prompt, job.cancel)
            record = self.store.save(
                job.user, result.to_dict() | {
                    "job_id": job.job_id, "delivery": "pending",
                    "model": job.model or getattr(result, "model", ""),
                }
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
            now = time.monotonic()
            if now < self._next_id_reply:
                return
            self._next_id_reply = now + 2.0
            self.queue_reply(chat_id, f"🆔 你的 Telegram 数字 ID：{user}")
            return
        if user not in self.settings.allowed or sender.get("is_bot") is True:
            return
        if command in {"/start", "/help"}:
            self.queue_reply(
                chat_id,
                "🤖 Antigravity Telegram Remote\n\n"
                "💬 直接发送文本：发送新任务给 agy 执行\n"
                "🧠 /model - 查看或切换模型（支持 14 种官方模型与快捷别名）\n"
                "📊 /status - 查看当前任务执行状态与生效模型\n"
                "🛑 /cancel - 立即取消任务并清理进程\n"
                "📜 /last - 取回本人最近结果（不重新执行）\n"
                "🆔 /id - 查看你的 Telegram 数字 ID\n"
                "❓ /help - 显示命令帮助说明\n\n"
                "💡 提示：每条普通消息独立执行，不保留历史上下文。",
            )
            return
        if command == "/status":
            current_model = self.store.get_model(user) or self.settings.model
            model_info = f" [模型：{current_model}]" if current_model else ""
            disk_info = ""
            try:
                import shutil
                usage = shutil.disk_usage(self.settings.workspace)
                free_gb = usage.free / (1024 ** 3)
                disk_info = f"\n💾 工作空间可用磁盘：{free_gb:.1f} GB"
            except Exception:
                pass
            if self.slot and self.slot.user == user:
                job_model = f"[{self.slot.model}] " if self.slot.model else ""
                text = f"⏳ 任务 {self.slot.job_id}：{job_model}" + (
                    "正在取消并清理。" if self.slot.cancel.is_set() else "运行或回传中。"
                )
            elif self.runner.blocked:
                text = "⚠️ 已暂停新任务：进程清理或结果保存发生异常，请检查并重启服务。"
            else:
                text = f"ℹ️ 你当前没有任务{model_info}。" + ("\n⚠️ 工作目录正被其他白名单用户的任务占用。" if self.slot else "")
            text += disk_info
            self.queue_reply(chat_id, text)
            return
        if command == "/cancel":
            job = self.slot
            if job is not None and job.user == user:
                job.cancel.set()
                self.queue_reply(chat_id, "🛑 已请求取消。会清理任务进程；已经发生的修改不会自动撤销。")
            else:
                self.queue_reply(chat_id, "ℹ️ 你当前没有可取消的任务。")
            return
        if command == "/last":
            try:
                record = self.store.load(user)
                text = describe(record) if record else "ℹ️ 没有可取回的结果，或记录已过保留期。"
            except (OSError, ValueError):
                text = "⚠️ 无法读取最近结果，请检查服务器状态。"
            self.queue_reply(chat_id, text)
            return
        if command == "/model":
            parts = text.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target or target.lower() in {"show", "current", "status", "list", "help"}:
                current_raw = self.store.get_model(user) or self.settings.model or ""
                current_display = current_raw or "默认（由 agy 决定）"
                lines = [
                    f"当前生效模型：{current_display}\n",
                    "切换指令：/model <模型名或别名>",
                    "恢复默认：/model default\n",
                    "官方支持的模型全列表（共 14 种）：",
                ]
                current_family = None
                for family, mid, desc in OFFICIAL_MODELS:
                    if family != current_family:
                        lines.append(f"\n【{family}】")
                        current_family = family
                    if current_raw == mid:
                        lines.append(f"👉 [当前使用] {mid} ({desc})")
                    else:
                        lines.append(f"• {mid} ({desc})")
                lines.append("\n快捷别名：3.8, 3.7, 3.6, pro, sonnet, opus, 120b 等")
                lines.append("💡 也支持直接输入任何未来或自定义的有效模型名称。")
                self.queue_reply(chat_id, "\n".join(lines))
                return
            if target.lower() in {"default", "reset", "auto", "clear"}:
                self.store.set_model(user, None)
                self.queue_reply(chat_id, "🔄 已恢复为默认模型（由 agy 决定）。\n💡 如需切换可随时使用 /model <模型名或别名>")
                return
            resolved = resolve_model(target)
            if not MODEL_RE.fullmatch(resolved):
                self.queue_reply(
                    chat_id,
                    "⚠️ 模型名称格式不正确。仅支持 2..64 个字母、数字、点、下划线与连字符。",
                )
                return
            self.store.set_model(user, resolved)
            alias_note = f"（由别名 '{target}' 解析）" if resolved != target else ""
            self.queue_reply(chat_id, f"🎯 已切换模型为：{resolved}{alias_note}\n🚀 后续任务将使用此模型。")
            return
        if command:
            self.queue_reply(chat_id, "⚠️ 不支持这个控制命令。发送 /help 查看可用指令。")
            return
        if len(text) > self.settings.max_prompt or "\x00" in text:
            self.queue_reply(chat_id, f"⚠️ 任务过长或含非法字符，最多 {self.settings.max_prompt} 个字符。")
            return
        if self.runner.blocked:
            self.queue_reply(chat_id, "⚠️ 新任务已暂停，请检查服务器并重启服务。")
            return
        if self.slot is not None:
            self.queue_reply(chat_id, "⚠️ 工作目录已有任务，请等待完成，或由任务发起者发送 /cancel。")
            return

        user_model = self.store.get_model(user) or self.settings.model or ""
        job = Job(user, chat_id, model=user_model)
        self.slot = job  # reserve before first await
        try:
            self.store.maintain()
            self.store.save(user, {
                "job_id": job.job_id, "outcome": "running", "delivery": "pending",
                "detail": "任务准备或执行中；服务中断时不会自动重试。",
                "model": job.model,
            })
            job.worker = asyncio.create_task(self._accept_and_work(job, text))
        except Exception:
            self.slot = None
            self.runner.blocked = True
            LOG.error("job_prepare_failed id=%s", job.job_id)
            self.queue_reply(chat_id, "任务准备失败，未启动 agy。请检查服务器存储和权限。")

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

    async def initialize_with_retry(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                await self.initialize()
                return
            except TelegramError as error:
                if error.code != 0 and error.code != 429 and not 500 <= error.code < 600:
                    raise
                LOG.warning("telegram_init_retry code=%s", error.code)
                try:
                    await asyncio.wait_for(
                        self.stop.wait(), max(backoff, float(error.retry_after)))
                except asyncio.TimeoutError:
                    pass
                backoff = min(30.0, backoff * 2)

    async def run(self) -> None:
        try:
            await self.initialize_with_retry()
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
            if self.reply_worker is not None:
                self.reply_worker.cancel()
                await asyncio.gather(self.reply_worker, return_exceptions=True)
            if job is not None and job.worker is not None:
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
