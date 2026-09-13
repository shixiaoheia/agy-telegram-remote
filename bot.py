#!/usr/bin/env python3
"""Private-chat Telegram bridge. One workspace, one job, no automatic task rerun."""
from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import re
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


def clean_model_reply(text: str) -> str:
    """Make Markdown-oriented model output comfortable to read as Telegram plain text."""
    lines: list[str] = []
    in_code_block = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            lines.append(line)
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "• ", line)
        line = re.sub(r"(?<!\w)(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1", r"\2", line)
        line = re.sub(r"^\s*>\s?", "│ ", line)
        lines.append(line)
    return "\n".join(lines).strip()


INLINE_MARKDOWN = re.compile(
    r"\[([^\]\n]+)]\((https?://[^\s)]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|"
    r"~~([^~\n]+)~~|(?<!\w)\*([^*\n]+)\*|(?<!\w)_([^_\n]+)_"
)


def render_inline_markdown(text: str) -> str:
    """Render the small, safe Markdown subset supported by Telegram HTML."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    result: list[str] = []
    cursor = 0
    for match in INLINE_MARKDOWN.finditer(text):
        result.append(html.escape(text[cursor:match.start()]))
        link, href, code, bold1, bold2, strike, italic1, italic2 = match.groups()
        if link is not None:
            result.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(link)}</a>')
        elif code is not None:
            result.append(f"<code>{html.escape(code)}</code>")
        elif bold1 is not None or bold2 is not None:
            result.append(f"<b>{html.escape(bold1 if bold1 is not None else bold2)}</b>")
        elif strike is not None:
            result.append(f"<s>{html.escape(strike)}</s>")
        else:
            result.append(f"<i>{html.escape(italic1 if italic1 is not None else italic2)}</i>")
        cursor = match.end()
    result.append(html.escape(text[cursor:]))
    return "".join(result)


MERMAID_START = re.compile(
    r"^\s*(?:flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|mindmap|timeline)\b",
    re.IGNORECASE,
)


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_table_separator(line: str) -> bool:
    cells = table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def render_markdown_html(text: str) -> str:
    """Convert common model Markdown to Telegram's conservative HTML subset."""
    lines: list[str] = []
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    code_lines: list[str] = []
    code_language = ""
    in_code_block = False
    index = 0
    while index < len(raw_lines):
        raw_line = raw_lines[index]
        if (raw_line.strip().startswith("|") and raw_line.strip().endswith("|")
                and index + 1 < len(raw_lines) and is_table_separator(raw_lines[index + 1])):
            headers = table_cells(raw_line)
            index += 2
            while (index < len(raw_lines) and raw_lines[index].strip().startswith("|")
                   and raw_lines[index].strip().endswith("|")):
                cells = table_cells(raw_lines[index])
                fields = [
                    f"<b>{render_inline_markdown(header)}</b>：{render_inline_markdown(value)}"
                    for header, value in zip(headers, cells)
                ]
                lines.append("\n".join(fields))
                index += 1
            continue
        if MERMAID_START.match(raw_line):
            diagram = [raw_line]
            index += 1
            while index < len(raw_lines) and raw_lines[index].strip():
                diagram.append(raw_lines[index])
                index += 1
            lines.append(f"<pre><code>{html.escape(chr(10).join(diagram))}</code></pre>")
            continue
        fence = re.match(r"^\s*```([A-Za-z0-9_+-]{0,32})\s*$", raw_line)
        if fence:
            if in_code_block:
                language = f' class="language-{html.escape(code_language, quote=True)}"' if code_language else ""
                lines.append(f"<pre><code{language}>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                code_language = ""
                in_code_block = False
            else:
                code_language = fence.group(1)
                in_code_block = True
            index += 1
            continue
        if in_code_block:
            code_lines.append(raw_line)
            index += 1
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", raw_line)
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", raw_line)
        quote = re.match(r"^\s*>\s?(.*)$", raw_line)
        if heading:
            lines.append(f"<b>{render_inline_markdown(heading.group(1))}</b>")
        elif bullet:
            lines.append(f"• {render_inline_markdown(bullet.group(1))}")
        elif quote:
            lines.append(f"│ {render_inline_markdown(quote.group(1))}")
        elif raw_line.strip() in {"---", "***", "___"}:
            lines.append("")
        else:
            lines.append(render_inline_markdown(raw_line))
        index += 1
    if in_code_block:
        language = f' class="language-{html.escape(code_language, quote=True)}"' if code_language else ""
        lines.append(f"<pre><code{language}>{html.escape(chr(10).join(code_lines))}</code></pre>")
    return "\n".join(lines).strip()


@dataclass
class Job:
    user: int
    chat: int
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    model: str = ""
    conversation_id: str = ""
    effort: str = ""
    mode: str = ""
    started_at: float = field(default_factory=time.monotonic)
    status_message_id: int | None = None
    progress_task: asyncio.Task | None = None


def describe(record: dict) -> str:
    outcome = str(record.get("outcome", "error"))
    title = LABELS.get(outcome, "结果需核对")
    model = record.get("model", "")
    model_tag = f"｜{model}" if model else ""
    duration = record.get("duration_seconds")
    duration_tag = f"｜⏱️ {duration}s" if isinstance(duration, (int, float)) and duration > 0 else ""

    parts = [f"{title}{model_tag}{duration_tag}"]
    parts.append("━━━━━━━━━━━━━━━━━━━━")

    if record.get("text"):
        parts.append(clean_model_reply(str(record["text"])))
        parts.append("━━━━━━━━━━━━━━━━━━━━")
    elif record.get("detail"):
        parts.append(str(record["detail"]))
        parts.append("━━━━━━━━━━━━━━━━━━━━")

    stats = []
    if isinstance(duration, (int, float)) and duration > 0:
        stats.append(f"• ⏱️ 执行耗时：{duration}s")
    if model:
        stats.append(f"• 🏷️ 选用模型：{model}")
    effort = record.get("effort")
    if effort:
        stats.append(f"• ⚡ 思考强度：{effort.capitalize()}")
    mode = record.get("mode")
    if mode:
        mode_name = "推演规划 (Plan)" if mode == "plan" else "落地编辑 (Accept-Edits)"
        stats.append(f"• 📋 执行模式：{mode_name}")
    input_tok = record.get("input_tokens", 0)
    output_tok = record.get("output_tokens", 0)
    total_tok = record.get("total_tokens", 0)
    if total_tok:
        stats.append(f"• 📈 Token 消耗：输入 {input_tok:,} ｜ 输出 {output_tok:,} ｜ 总计 {total_tok:,}")
    num_turns = record.get("num_turns", 0)
    if num_turns:
        stats.append(f"• 🧠 会话轮次：第 {num_turns} 轮")

    if stats:
        parts.append("📊 运行统计：\n" + "\n".join(stats))

    help_text = CATEGORY_HELP.get(record.get("category"))
    if help_text:
        parts.append(f"💡 建议：{help_text}")

    if outcome not in {"success", "not_started", "running"}:
        parts.append("ℹ️ 没有自动重跑任务。请先核对工作目录；/last 只取回记录，不重新执行。")

    return "\n\n".join(parts)


def describe_html(record: dict) -> str:
    """Render an agy result as Telegram HTML without trusting model-provided tags."""
    if not record.get("text"):
        return html.escape(describe(record))
    marker = "AGY_MODEL_REPLY_MARKER"
    plain = dict(record)
    plain["text"] = marker
    return html.escape(describe(plain)).replace(marker, render_markdown_html(str(record["text"])))


def format_file_entry(is_dir: bool, name: str, size: int, mtime: float) -> str:
    import datetime
    dt_str = datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
    if is_dir:
        return f"📁 `{name}/` ｜ {dt_str}"

    lower = name.lower()
    if lower.endswith(".py"):
        icon = "🐍"
    elif lower.endswith((".sh", ".bash", ".zsh")):
        icon = "🐚"
    elif lower.endswith((".md", ".markdown", ".rst")):
        icon = "📝"
    elif lower.endswith((".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".env")):
        icon = "⚙️"
    elif lower.endswith((".log", ".out", ".err")):
        icon = "📋"
    elif lower.endswith((".txt", ".csv", ".tsv")):
        icon = "📄"
    elif lower.endswith((".tar", ".gz", ".zip", ".7z", ".bz2", ".xz")):
        icon = "📦"
    elif lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        icon = "🖼️"
    elif lower.endswith((".db", ".sqlite", ".sqlite3", ".sql")):
        icon = "🗄️"
    elif lower.startswith(".") or "lock" in lower:
        icon = "🔒"
    else:
        icon = "📄"

    if size < 1024:
        size_str = f"{size} B"
    elif size < 1024 * 1024:
        size_str = f"{size / 1024:.1f} KB"
    else:
        size_str = f"{size / (1024 * 1024):.1f} MB"
    return f"{icon} `{name}` ({size_str}) ｜ {dt_str}"


def system_status(workspace: Path, start_time: float) -> str:
    import platform
    import shutil
    import socket

    hostname = socket.gethostname()
    sys_name = platform.system()
    release = platform.release()
    machine = platform.machine()
    python_ver = platform.python_version()

    distro_name = f"{sys_name} {release}"
    try:
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        distro_name = line.split("=", 1)[1].strip().strip('"')
                        break
    except Exception:
        pass

    uptime_str = "未知"
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            total_seconds = float(f.read().split()[0])
            days = int(total_seconds // 86400)
            hours = int((total_seconds % 86400) // 3600)
            mins = int((total_seconds % 3600) // 60)
            uptime_str = f"{days} 天 {hours} 小时 {mins} 分钟" if days > 0 else f"{hours} 小时 {mins} 分钟"
    except Exception:
        pass

    cores = os.cpu_count() or 1
    load_str = "未知"
    try:
        l1, l5, l15 = os.getloadavg()
        load_str = f"{l1:.2f}, {l5:.2f}, {l15:.2f} ({cores} 核)"
    except Exception:
        pass

    mem_str = "未知"
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_kb = meminfo.get("MemTotal", 0)
            avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0) + meminfo.get("Buffers", 0) + meminfo.get("Cached", 0))
            if total_kb > 0:
                used_kb = total_kb - avail_kb
                total_gb = total_kb / (1024 * 1024)
                used_gb = used_kb / (1024 * 1024)
                pct = (used_kb / total_kb) * 100
                mem_str = f"{used_gb:.2f} GB / {total_gb:.2f} GB ({pct:.1f}%)"
    except Exception:
        pass

    disk_str = "未知"
    try:
        usage = shutil.disk_usage(workspace)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
        pct = (usage.used / usage.total) * 100
        disk_str = f"剩余 {free_gb:.1f} GB / 总计 {total_gb:.1f} GB ({pct:.1f}% 已用)"
    except Exception:
        pass

    pid = os.getpid()
    bot_uptime_sec = int(time.time() - start_time)
    b_hours = bot_uptime_sec // 3600
    b_mins = (bot_uptime_sec % 3600) // 60
    b_secs = bot_uptime_sec % 60
    bot_up_str = f"{b_hours}h {b_mins}m {b_secs}s" if b_hours > 0 else f"{b_mins}m {b_secs}s"

    rss_str = ""
    try:
        pagesize = os.sysconf("SC_PAGE_SIZE")
        with open(f"/proc/{pid}/statm", "r", encoding="utf-8") as f:
            rss_pages = int(f.read().split()[1])
            rss_mb = (rss_pages * pagesize) / (1024 * 1024)
            rss_str = f" ｜ 常驻内存 {rss_mb:.1f} MB"
    except Exception:
        pass

    lines = [
        f"🖥️ 系统运行状态 ｜ 主机：{hostname}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"• 🐧 操作系统：{distro_name} ({machine})",
        f"• ⏱️ 系统运行：{uptime_str}",
        f"• ⚡ CPU 负载：{load_str}",
        f"• 💾 物理内存：{mem_str}",
        f"• 💽 工作区磁盘：{disk_str}",
        f"• 🤖 守护进程：PID {pid}{rss_str} ｜ 已运行 {bot_up_str}",
        f"• 🐍 Python 版本：v{python_ver}",
        "━━━━━━━━━━━━━━━━━━━━",
        "💡 实时读取 Linux /proc 与底层系统信息，零外部依赖。",
    ]
    return "\n".join(lines)


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
        self.replies: asyncio.Queue[tuple] = asyncio.Queue(maxsize=32)
        self.reply_worker: asyncio.Task | None = None
        self._next_id_reply = 0.0

    def _is_allowed(self, user: int) -> bool:
        return user in self.settings.allowed or user in self.store.allowed

    async def send_text(self, chat: int, text: str, reply_markup: dict | None = None) -> bool:
        try:
            chunks = chunks_utf16(self.store.redact(text))
            total = len(chunks)
            for i, chunk in enumerate(chunks):
                suffix = f"\n\n📄 [第 {i+1}/{total} 页]" if total > 1 else ""
                markup = reply_markup if i == total - 1 else None
                try:
                    await self.api.send(chat, chunk + suffix, reply_markup=markup)
                except TypeError:
                    await self.api.send(chat, chunk + suffix)
            return True
        except TelegramError as error:
            LOG.warning("telegram_delivery_failed code=%s", error.code)
            return False

    async def send_html(self, chat: int, text: str) -> bool:
        # HTML must not be split mid-tag. Long replies fall back to readable plain text.
        if len(text.encode("utf-16-le")) // 2 > 3500:
            return await self.send_text(chat, clean_model_reply(re.sub(r"<[^>]+>", "", text)))
        try:
            await self.api.send(chat, text, parse_mode="HTML")
            return True
        except TypeError:
            return await self.send_text(chat, clean_model_reply(re.sub(r"<[^>]+>", "", text)))
        except TelegramError as error:
            LOG.warning("telegram_html_delivery_failed code=%s", error.code)
            # A malformed model fragment may be rejected by Telegram HTML parsing.
            # Only that definite client-side failure safely falls back to plain text.
            if error.code == 400:
                return await self.send_text(chat, clean_model_reply(re.sub(r"<[^>]+>", "", text)))
            return False

    def queue_reply(self, chat: int, text: str, reply_markup: dict | None = None,
                    html_mode: bool = False) -> None:
        # Control replies must never hold up update ingestion or cancellation.
        if self.stop.is_set():
            return
        try:
            self.replies.put_nowait((chat, text, reply_markup, html_mode))
        except asyncio.QueueFull:
            return
        if self.reply_worker is None or self.reply_worker.done():
            self.reply_worker = asyncio.create_task(self._send_replies())

    async def _send_replies(self) -> None:
        while not self.replies.empty():
            item = self.replies.get_nowait()
            if len(item) == 4:
                chat, text, reply_markup, html_mode = item
            else:
                chat, text = item
                reply_markup = None
                html_mode = False
            try:
                if html_mode:
                    await self.send_html(chat, text)
                else:
                    await self.send_text(chat, text, reply_markup=reply_markup)
            finally:
                self.replies.task_done()

    async def _accept_and_work(self, job: Job, prompt: str) -> None:
        try:
            model_info = f"（模型：{job.model}）" if job.model else ""
            effort_info = f"\n⚡ 思考强度：{job.effort.capitalize()}" if job.effort else ""
            mode_name = "推演规划 (Plan)" if job.mode == "plan" else ("落地编辑 (Accept-Edits)" if job.mode == "accept-edits" else job.mode)
            mode_info = f"\n📋 执行模式：{mode_name}" if job.mode else ""
            conv = self.store.get_conversation(job.user)
            conv_tag = ""
            if conv and conv.get("conversation_id"):
                turns = conv.get("num_turns", 1)
                conv_tag = f"\n🧠 会话记忆：已关联上下文 (第 {turns + 1} 轮)"
            else:
                conv_tag = "\n🧠 会话记忆：全新独立会话"

            accept_msg = (
                f"🚀 已接收任务{model_info}，准备调用 agy。\n"
                f"━━━━━━━━━━━━━━━━━━━━{conv_tag}{effort_info}{mode_info}\n"
                f"⏳ 正在调用 Antigravity 执行任务，请稍候...\n\n"
                f"进度会每 5 秒自动更新；需要停止时请点下方按钮。"
            )
            cancel_markup = {"inline_keyboard": [[{
                "text": "🛑 取消任务", "callback_data": f"cancel:{job.job_id}"
            }]]}
            try:
                sent = await self.api.send(job.chat, accept_msg, reply_markup=cancel_markup)
                job.status_message_id = sent.get("message_id") if isinstance(sent, dict) else None
                accepted = True
            except TelegramError:
                accepted = False
            if not accepted or self.stop.is_set() or job.cancel.is_set():
                self.store.save(job.user, {
                    "job_id": job.job_id, "outcome": "not_started",
                    "detail": "确认消息未成功投递、任务已取消或服务正在停止；没有启动 agy。",
                    "delivery": "sent" if accepted else "failed",
                    "model": job.model,
                    "effort": job.effort,
                    "mode": job.mode,
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
            job.progress_task = asyncio.create_task(self._refresh_progress(job))
            try:
                result = await self.runner.run(
                    prompt, job.cancel, model=job.model or None,
                    conversation_id=job.conversation_id or None,
                    effort=job.effort or None, mode=job.mode or None,
                )
            except TypeError:
                try:
                    result = await self.runner.run(
                        prompt, job.cancel, model=job.model or None,
                        conversation_id=job.conversation_id or None,
                    )
                except TypeError:
                    try:
                        result = await self.runner.run(prompt, job.cancel, model=job.model or None)
                    except TypeError:
                        result = await self.runner.run(prompt, job.cancel)

            if result.outcome == "success" and getattr(result, "conversation_id", None):
                self.store.set_conversation(job.user, result.conversation_id, getattr(result, "num_turns", 1))
            if getattr(result, "total_tokens", 0) > 0:
                self.store.record_usage(
                    job.user,
                    getattr(result, "input_tokens", 0),
                    getattr(result, "output_tokens", 0),
                    getattr(result, "thinking_tokens", 0),
                )

            record = self.store.save(
                job.user, result.to_dict() | {
                    "job_id": job.job_id, "delivery": "pending",
                    "model": job.model or getattr(result, "model", ""),
                    "effort": job.effort or getattr(result, "effort", ""),
                    "mode": job.mode or getattr(result, "mode", ""),
                }
            )
            LOG.info("job_finished id=%s outcome=%s", job.job_id, result.outcome)
            await self._finish_progress(job, result.outcome)
            delivered = await self.send_html(job.chat, describe_html(record))
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
            if job.progress_task:
                job.progress_task.cancel()
                await asyncio.gather(job.progress_task, return_exceptions=True)
            if self.slot is job:
                self.slot = None

    def _progress_text(self, job: Job) -> str:
        elapsed = max(0, int(time.monotonic() - job.started_at))
        state = "正在取消并清理进程..." if job.cancel.is_set() else "正在执行任务..."
        return (f"⏳ {state}\n━━━━━━━━━━━━━━━━━━━━\n"
                f"• 已运行：{elapsed}s\n"
                f"• 每 5 秒自动更新\n━━━━━━━━━━━━━━━━━━━━")

    async def _refresh_progress(self, job: Job) -> None:
        while not self.stop.is_set() and not job.cancel.is_set():
            await asyncio.sleep(5)
            if job.status_message_id is None or self.slot is not job:
                continue
            try:
                await self.api.edit(job.chat, job.status_message_id, self._progress_text(job), {
                    "inline_keyboard": [[{"text": "🛑 取消任务", "callback_data": f"cancel:{job.job_id}"}]]
                })
            except (TelegramError, AttributeError):
                return

    async def _finish_progress(self, job: Job, outcome: str) -> None:
        if job.status_message_id is None:
            return
        try:
            await self.api.edit(job.chat, job.status_message_id,
                                f"{LABELS.get(outcome, '任务已结束')}｜已停止进度更新。")
        except (TelegramError, AttributeError):
            pass

    def _show_effort_picker(self, chat_id: int, user: int, model_label: str) -> None:
        curr = self.store.get_effort(user)
        lines = [
            f"🎯 已选择模型：{model_label}",
            "━━━━━━━━━━━━━━━━━━━━",
            "下一步：请选择该模型的思考强度。",
            "• ⚡ 极速：适合简单问答与快速修改",
            "• ⚖️ 均衡：速度与推理深度兼顾",
            "• 🧠 深度：适合复杂分析与大型重构",
        ]
        keyboard = {"inline_keyboard": [
            [
                {"text": f"{'🔘' if curr == 'low' else '⚪'} ⚡ 极速", "callback_data": "effort:low"},
                {"text": f"{'🔘' if curr == 'medium' else '⚪'} ⚖️ 均衡", "callback_data": "effort:medium"},
            ],
            [
                {"text": f"{'🔘' if curr == 'high' else '⚪'} 🧠 深度", "callback_data": "effort:high"},
                {"text": f"{'🔘' if not curr else '⚪'} 🔄 默认", "callback_data": "effort:default"},
            ],
        ]}
        self.queue_reply(chat_id, "\n".join(lines), reply_markup=keyboard)

    async def handle_callback(self, cq: dict) -> None:
        cq_id = str(cq.get("id") or "")
        sender = cq.get("from") or {}
        user = sender.get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id") or user
        data = str(cq.get("data") or "")

        if type(user) is not int or not self._is_allowed(user) or sender.get("is_bot") is True:
            if cq_id and hasattr(self.api, "answer_callback_query"):
                await self.api.answer_callback_query(cq_id, text="⚠️ 无操作权限", show_alert=True)
            return

        if data.startswith("cancel:"):
            job = self.slot
            if job is not None and job.user == user and data == f"cancel:{job.job_id}":
                job.cancel.set()
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    await self.api.answer_callback_query(cq_id, text="🛑 已请求取消")
                self.queue_reply(chat_id, "🛑 已请求取消。正在清理任务进程；已经发生的修改不会自动撤销。")
            elif cq_id and hasattr(self.api, "answer_callback_query"):
                await self.api.answer_callback_query(cq_id, text="该任务已结束或不可取消")
            return

        if data.startswith("model:"):
            target = data[6:].strip()
            if target.lower() in {"default", "reset", "auto", "clear"}:
                self.store.set_model(user, None)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    await self.api.answer_callback_query(cq_id, text="🔄 已恢复为默认模型")
                self._show_effort_picker(chat_id, user, "默认（由 agy 决定）")
                return
            resolved = resolve_model(target)
            if not MODEL_RE.fullmatch(resolved):
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    await self.api.answer_callback_query(cq_id, text="⚠️ 模型名称格式错误", show_alert=True)
                return
            self.store.set_model(user, resolved)
            if cq_id and hasattr(self.api, "answer_callback_query"):
                await self.api.answer_callback_query(cq_id, text=f"🎯 已切换至 {resolved}")
            self._show_effort_picker(chat_id, user, resolved)
            return

        if data.startswith("effort:"):
            target = data[7:].strip().lower()
            if target in {"default", "reset", "clear"}:
                self.store.set_effort(user, None)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    await self.api.answer_callback_query(cq_id, text="🔄 已恢复默认思考强度")
                self.queue_reply(chat_id, "🔄 已恢复为默认思考强度。")
                return
            if target in {"low", "medium", "high"}:
                self.store.set_effort(user, target)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    await self.api.answer_callback_query(cq_id, text=f"🎯 已设置思考强度为: {target.capitalize()}")
                self.queue_reply(chat_id, f"🎯 已切换思考强度为：`{target.capitalize()}`\n🚀 后续任务将以此强度调用 agy。")
                return

        if data.startswith("mode:"):
            target = data[5:].strip().lower()
            if target in {"default", "reset", "clear"}:
                self.store.set_mode(user, None)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    await self.api.answer_callback_query(cq_id, text="🔄 已恢复默认执行模式")
                self.queue_reply(chat_id, "🔄 已恢复为默认执行模式。")
                return
            if target in {"plan", "accept-edits"}:
                self.store.set_mode(user, target)
                name = "推演规划模式 (Plan)" if target == "plan" else "落地编辑模式 (Accept-Edits)"
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    await self.api.answer_callback_query(cq_id, text=f"🎯 已切换为: {name}")
                self.queue_reply(chat_id, f"🎯 已切换模式为：`{name}`\n🚀 后续任务将使用此模式。")
                return

    async def handle(self, update: dict) -> None:
        if self.stop.is_set():
            return
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self.handle_callback(callback_query)
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
        if not self._is_allowed(user) or sender.get("is_bot") is True:
            return
        if command in {"/start", "/help"}:
            self.queue_reply(
                chat_id,
                "🤖 Antigravity Telegram Remote\n━━━━━━━━━━━━━━━━━━━━\n"
                "💬 直接发送文字：向 AI 助手提问或分派任务\n\n"
                "【🧠 模型与推理配置】\n"
                "• 🧠 /model - 选择模型后继续选择思考强度\n"
                "• 📋 /mode - 切换执行模式（Accept-Edits 落地 / Plan 推演规划）\n\n"
                "【💬 会话与用量管理】\n"
                "• 🔄 /new 或 /reset - 重置对话记忆，开启全新独立对话\n"
                "• 📊 /usage - 查看 Token 消耗明细与对话轮数\n"
                "• 📈 /status - 查看任务执行状态、记忆与工作空间可用磁盘\n\n"
                "【🖥️ 系统与运维管理】\n"
                "• 📁 /ls 或 /files - 速览工作空间最近修改的文件列表\n"
                "• 🖥️ /sys 或 /system - 实时查看 VPS 硬件负载、CPU、内存与运行时间\n"
                "• 👥 /whitelist - 管理授权白名单用户（仅主管理员）\n"
                "• 🔄 /restart - 重新载入并启动守护进程（仅主管理员）\n\n"
                "【🛑 任务控制与基础】\n"
                "• 🛑 /cancel - 立即取消正在执行的任务\n"
                "• 📜 /last - 查看最近一条任务的执行结果\n"
                "• 🆔 /id - 查看你的 Telegram 数字 ID\n"
                "• ❓ /help - 显示帮助说明\n━━━━━━━━━━━━━━━━━━━━\n"
                "✨ 零依赖纯 Python 构建 ｜ 原生支持多轮上下文对话记忆！",
            )
            return
        if command in {"/new", "/reset"}:
            self.store.reset_conversation(user)
            self.queue_reply(
                chat_id,
                "🧠 对话记忆已重置！\n━━━━━━━━━━━━━━━━━━━━\n已清空当前上下文，下一条消息将开启全新对话。",
            )
            return
        if command == "/usage":
            usage = self.store.get_usage(user)
            conv = self.store.get_conversation(user)
            conv_info = "无活跃上下文（发送任意消息自动开启）"
            if conv and conv.get("conversation_id"):
                cid = conv["conversation_id"]
                short_cid = f"{cid[:8]}...{cid[-4:]}" if len(cid) > 16 else cid
                turns = conv.get("num_turns", 1)
                conv_info = f"`{short_cid}`（已连续对话 {turns} 轮）"
            self.queue_reply(
                chat_id,
                f"📊 Token 用量统计报告\n━━━━━━━━━━━━━━━━━━━━\n"
                f"💬 当前会话：{conv_info}\n\n"
                f"📈 历史累计资源消耗：\n"
                f"• 💬 累计对话轮次：{usage.get('total_turns', 0)} 轮\n"
                f"• 📥 输入 Token：{usage.get('total_input_tokens', 0):,}\n"
                f"• 📤 输出 Token：{usage.get('total_output_tokens', 0):,}\n"
                f"• 🧠 思考 Token：{usage.get('total_thinking_tokens', 0):,}\n"
                f"• 📊 总计 Token：{(usage.get('total_input_tokens', 0) + usage.get('total_output_tokens', 0)):,}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 发送 /new 或 /reset 可重置当前会话上下文。",
            )
            return
        if command == "/status":
            current_model = self.store.get_model(user) or self.settings.model
            model_info = f" [模型：{current_model}]" if current_model else ""
            effort = self.store.get_effort(user)
            effort_info = f"\n⚡ 思考强度：{effort.capitalize()}" if effort else ""
            mode = self.store.get_mode(user)
            mode_name = "推演规划 (Plan)" if mode == "plan" else ("落地编辑 (Accept-Edits)" if mode == "accept-edits" else mode)
            mode_info = f"\n📋 执行模式：{mode_name}" if mode else ""
            conv = self.store.get_conversation(user)
            conv_info = ""
            if conv and conv.get("conversation_id"):
                cid = conv["conversation_id"]
                short_cid = f"{cid[:8]}...{cid[-4:]}" if len(cid) > 16 else cid
                turns = conv.get("num_turns", 1)
                conv_info = f"\n🧠 记忆会话：`{short_cid}` (第 {turns} 轮)"
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
                text = f"⏳ 当前任务：{job_model}" + (
                    "正在取消并清理。" if self.slot.cancel.is_set() else "运行或回传中。"
                )
            elif self.runner.blocked:
                text = "⚠️ 已暂停新任务：进程清理或结果保存发生异常，请检查并重启服务。"
            else:
                text = f"ℹ️ 你当前没有任务{model_info}。" + ("\n⚠️ 工作目录正被其他白名单用户的任务占用。" if self.slot else "")
            text += conv_info + effort_info + mode_info + disk_info
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
                if record:
                    self.queue_reply(chat_id, describe_html(record), html_mode=True)
                    return
                text = "ℹ️ 没有可取回的结果，或记录已过保留期。"
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
                    f"🧠 AI 模型管理 ｜ 当前生效模型：{current_display}",
                    "━━━━━━━━━━━━━━━━━━━━",
                    "• 切换指令：/model <模型名或别名>",
                    "• 恢复默认：/model default",
                    "━━━━━━━━━━━━━━━━━━━━",
                    "【官方支持的模型全列表（共 14 种）】",
                ]
                current_family = None
                for family, mid, desc in OFFICIAL_MODELS:
                    if family != current_family:
                        lines.append(f"\n📂 【{family}】")
                        current_family = family
                    if current_raw == mid:
                        lines.append(f"👉 [当前使用] {mid} ({desc})")
                    else:
                        lines.append(f"• {mid} ({desc})")
                lines.append("\n━━━━━━━━━━━━━━━━━━━━")
                lines.append("⚡ 快捷别名：3.8, 3.7, 3.6, pro, sonnet, opus, 120b 等")
                lines.append("💡 也支持直接输入任何未来或自定义的有效模型名称。")
                lines.append("\n👇 点击下方按钮可直接一键切换模型：")
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": f"{'🔘' if current_raw == 'gemini-3.8-flash-high' else '⚪'} ✨ 3.8 Flash (推荐)", "callback_data": "model:gemini-3.8-flash-high"},
                            {"text": f"{'🔘' if current_raw == 'gemini-3.7-flash-high' else '⚪'} ⚡ 3.7 Flash", "callback_data": "model:gemini-3.7-flash-high"},
                        ],
                        [
                            {"text": f"{'🔘' if current_raw == 'gemini-3.1-pro-high' else '⚪'} 🧠 3.1 Pro (旗舰)", "callback_data": "model:gemini-3.1-pro-high"},
                            {"text": f"{'🔘' if current_raw == 'gemini-3.6-flash-high' else '⚪'} 💡 3.6 Flash", "callback_data": "model:gemini-3.6-flash-high"},
                        ],
                        [
                            {"text": f"{'🔘' if current_raw == 'claude-sonnet-4-6' else '⚪'} 🚀 Claude Sonnet", "callback_data": "model:claude-sonnet-4-6"},
                            {"text": f"{'🔘' if current_raw == 'claude-opus-4-6-thinking' else '⚪'} 🏆 Claude Opus", "callback_data": "model:claude-opus-4-6-thinking"},
                        ],
                        [
                            {"text": f"{'🔘' if current_raw == 'gpt-oss-120b-medium' else '⚪'} 🌐 GPT-OSS 120B", "callback_data": "model:gpt-oss-120b-medium"},
                            {"text": f"{'🔘' if not current_raw else '⚪'} 🔄 恢复系统默认", "callback_data": "model:default"},
                        ],
                    ]
                }
                self.queue_reply(chat_id, "\n".join(lines), reply_markup=keyboard)
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
        if command in {"/sys", "/system"}:
            self.queue_reply(chat_id, system_status(self.settings.workspace, self._started_at))
            return
        if command == "/restart":
            admin_id = self.settings.owner_id
            if user != admin_id:
                self.queue_reply(chat_id, f"⚠️ 仅主管理员（ID: {admin_id}）可以重启守护进程。")
                return
            if self.slot is not None and not self.slot.cancel.is_set():
                self.queue_reply(
                    chat_id,
                    "⚠️ 当前有正在执行的任务，请等待其完成或先发送 /cancel 后再重启。"
                )
                return
            self.queue_reply(chat_id, "🔄 守护进程正在重新载入并启动，请稍候约 3-5 秒后发送 /status 验证……")

            async def _do_restart():
                await asyncio.sleep(0.8)
                try:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception as err:
                    LOG.error("restart_execv_failed: %s", err)

            asyncio.create_task(_do_restart())
            return
        if command == "/whitelist":
            admin_id = self.settings.owner_id
            parts = text.split(maxsplit=2)
            subcmd = parts[1].lower() if len(parts) > 1 else ""
            target_str = parts[2].strip() if len(parts) > 2 else ""

            if not subcmd or subcmd in {"list", "show", "help"}:
                base_users = sorted(self.settings.allowed)
                extra_users = self.store.get_extra_whitelist()
                lines = [
                    "👥 白名单管理 ｜ 授权用户列表",
                    "━━━━━━━━━━━━━━━━━━━━",
                    "【🔒 基础配置白名单 (config.env)】",
                ]
                for uid in base_users:
                    tag = " 👑 (主管理员)" if uid == admin_id else ""
                    lines.append(f"• ID: `{uid}`{tag}")
                lines.append("\n【➕ 动态授权白名单 (whitelist.json)】")
                if extra_users:
                    for uid in extra_users:
                        lines.append(f"• ID: `{uid}`")
                else:
                    lines.append("• 暂无动态添加的用户")
                lines.extend([
                    "━━━━━━━━━━━━━━━━━━━━",
                    "💡 权限管理指令（仅主管理员可用）：",
                    "• ➕ 添加用户：/whitelist add <数字ID>",
                    "• 🗑️ 移除用户：/whitelist remove <数字ID>",
                ])
                self.queue_reply(chat_id, "\n".join(lines))
                return

            if user != admin_id:
                self.queue_reply(chat_id, f"⚠️ 仅主管理员（ID: {admin_id}）可管理动态白名单。")
                return

            if subcmd == "add":
                if not target_str.isdigit() or not (5 <= len(target_str) <= 16):
                    self.queue_reply(chat_id, "⚠️ 请输入合法的 Telegram 数字用户 ID（5-16 位正整数）。\n例如：/whitelist add 123456789")
                    return
                target_id = int(target_str)
                if target_id in self.settings.allowed:
                    self.queue_reply(chat_id, f"ℹ️ 用户 `{target_id}` 已在 config.env 基础配置中，无需重复添加。")
                    return
                ok = self.store.add_whitelist(target_id)
                if ok:
                    self.queue_reply(chat_id, f"✅ 已成功添加用户 `{target_id}` 到授权白名单！该用户现在可以私聊使用此 Bot。")
                else:
                    self.queue_reply(chat_id, f"ℹ️ 用户 `{target_id}` 已经在动态白名单中了。")
                return

            if subcmd in {"remove", "del", "delete", "rm"}:
                if not target_str.isdigit():
                    self.queue_reply(chat_id, "⚠️ 请指定要移除的 Telegram 数字用户 ID。\n例如：/whitelist remove 123456789")
                    return
                target_id = int(target_str)
                if target_id in self.settings.allowed:
                    self.queue_reply(chat_id, f"⚠️ 用户 `{target_id}` 属于 config.env 基础配置白名单，无法通过指令移除。")
                    return
                ok = self.store.remove_whitelist(target_id)
                if ok:
                    self.queue_reply(chat_id, f"🗑️ 已成功从动态白名单中移除用户 `{target_id}`。")
                else:
                    self.queue_reply(chat_id, f"⚠️ 未在动态白名单中找到用户 `{target_id}`。")
                return

            self.queue_reply(chat_id, "⚠️ 未知白名单指令。用法：\n• 查看：/whitelist\n• 添加：/whitelist add <ID>\n• 移除：/whitelist remove <ID>")
            return
        if command == "/mode":
            parts = text.split(maxsplit=1)
            target = parts[1].strip().lower() if len(parts) > 1 else ""
            if not target or target in {"show", "current", "list", "help"}:
                curr = self.store.get_mode(user)
                curr_display = "推演规划模式 (Plan)" if curr == "plan" else ("落地编辑模式 (Accept-Edits)" if curr == "accept-edits" else "默认（标准落地编辑）")
                lines = [
                    f"📋 执行模式设置 ｜ 当前：{curr_display}",
                    "━━━━━━━━━━━━━━━━━━━━",
                    "• 🛠️ 落地编辑模式 (accept-edits)：\n  AI 将实际在工作目录中创建、修改和执行代码文件。",
                    "• 📋 推演规划模式 (plan)：\n  AI 仅进行推演架构方案与执行步骤规划，不修改任何文件。",
                    "• 🔄 默认模式 (default)：\n  恢复为标准落地模式。",
                    "━━━━━━━━━━━━━━━━━━━━",
                    "💡 你可以点击下方按钮切换，或输入：/mode <plan|code|default>",
                ]
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": f"{'🔘' if curr == 'accept-edits' else '⚪'} 🛠️ 落地编辑", "callback_data": "mode:accept-edits"},
                            {"text": f"{'🔘' if curr == 'plan' else '⚪'} 📋 推演规划", "callback_data": "mode:plan"},
                        ],
                        [
                            {"text": f"{'🔘' if not curr else '⚪'} 🔄 恢复默认模式", "callback_data": "mode:default"},
                        ],
                    ]
                }
                self.queue_reply(chat_id, "\n".join(lines), reply_markup=keyboard)
                return

            if target in {"default", "reset", "clear", "auto"}:
                self.store.set_mode(user, None)
                self.queue_reply(chat_id, "🔄 已恢复为默认执行模式。")
                return
            if target in {"plan", "planning", "规划"}:
                self.store.set_mode(user, "plan")
                self.queue_reply(chat_id, "📋 已切换为【推演规划模式 (plan)】！\n💡 在此模式下，AI 将仅推演方案与计划，不会实际修改任何文件。")
                return
            if target in {"code", "edit", "edits", "accept-edits", "落地", "编辑"}:
                self.store.set_mode(user, "accept-edits")
                self.queue_reply(chat_id, "🛠️ 已切换为【落地编辑模式 (accept-edits)】！\n💡 在此模式下，AI 将直接在工作目录中修改并应用代码。")
                return
            self.queue_reply(chat_id, "⚠️ 不支持的执行模式。可选：`/mode plan` (推演规划), `/mode code` (落地编辑), `/mode default` (默认)")
            return
        if command in {"/ls", "/files"}:
            workspace = self.settings.workspace
            parts = text.split(maxsplit=1)
            subpath = parts[1].strip() if len(parts) > 1 else ""
            target_dir = workspace
            if subpath:
                clean_sub = subpath.lstrip("/\\")
                candidate = (workspace / clean_sub).resolve()
                try:
                    candidate.relative_to(workspace.resolve())
                except ValueError:
                    self.queue_reply(chat_id, "⚠️ 路径不合法：严禁越权访问工作空间以外的目录。")
                    return
                if not candidate.exists() or not candidate.is_dir():
                    self.queue_reply(chat_id, f"⚠️ 子目录 `{subpath}` 不存在或并非目录。")
                    return
                target_dir = candidate

            try:
                entries = []
                for p in target_dir.iterdir():
                    try:
                        st = p.stat()
                        entries.append((st.st_mtime, p.is_dir(), st.st_size, p.name))
                    except (OSError, PermissionError):
                        continue
                entries.sort(key=lambda x: (not x[1], -x[0]))
                total_count = len(entries)
                display_items = entries[:15]

                sub_tag = f" ｜ `{target_dir.relative_to(workspace)}/`" if target_dir != workspace else ""
                lines = [
                    f"📁 工作空间文件速览 ｜ `{workspace}`{sub_tag}",
                    "━━━━━━━━━━━━━━━━━━━━",
                ]
                if not display_items:
                    lines.append("（当前目录为空）")
                else:
                    for mtime, is_dir, size, name in display_items:
                        lines.append(format_file_entry(is_dir, name, size, mtime))

                lines.append("━━━━━━━━━━━━━━━━━━━━")
                import shutil
                try:
                    free_gb = shutil.disk_usage(workspace).free / (1024 ** 3)
                    lines.append(f"📊 目录总计 {total_count} 个项目 ｜ 💾 剩余可用磁盘：{free_gb:.1f} GB")
                except Exception:
                    lines.append(f"📊 目录总计 {total_count} 个项目")
                if target_dir == workspace and any(e[1] for e in entries):
                    lines.append("💡 发送 /ls <子目录> 可展开查看指定子目录。")
                self.queue_reply(chat_id, "\n".join(lines))
            except Exception:
                self.queue_reply(chat_id, "⚠️ 无法读取工作空间目录，请检查服务器权限。")
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
        conv = self.store.get_conversation(user)
        conv_id = conv.get("conversation_id", "") if conv else ""
        effort = self.store.get_effort(user) or ""
        mode = self.store.get_mode(user) or ""
        job = Job(user, chat_id, model=user_model, conversation_id=conv_id, effort=effort, mode=mode)
        self.slot = job  # reserve before first await
        try:
            self.store.maintain()
            self.store.save(user, {
                "job_id": job.job_id, "outcome": "running", "delivery": "pending",
                "detail": "任务准备或执行中；服务中断时不会自动重试。",
                "model": job.model,
                "effort": job.effort,
                "mode": job.mode,
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
        try:
            await self.api.set_commands([
                {"command": "help", "description": "查看帮助与使用说明"},
                {"command": "status", "description": "查看当前任务状态"},
                {"command": "cancel", "description": "取消正在执行的任务"},
                {"command": "model", "description": "选择 AI 模型"},
                {"command": "new", "description": "新建对话并清除上下文"},
                {"command": "usage", "description": "查看 Token 用量"},
                {"command": "last", "description": "查看最近一次结果"},
            ])
        except (TelegramError, AttributeError):
            LOG.warning("telegram_command_menu_unavailable")
        webhook = await self.api.call("getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise RuntimeError("An active webhook must be removed explicitly before polling")
        # Drop pre-start backlog. Negative offset is documented by Telegram.
        updates = await self.api.call(
            "getUpdates", offset=-1, limit=1, timeout=0, allowed_updates=["message", "callback_query"]
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
                        allowed_updates=["message", "callback_query"],
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
