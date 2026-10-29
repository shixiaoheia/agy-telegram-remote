#!/usr/bin/env python3
"""Private-chat Telegram bridge. One workspace, one job, no automatic task rerun."""
from __future__ import annotations

import argparse
import asyncio
import html
import logging
import math
import os
import re
import signal
import stat
import sys
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from agy_runner import Result, Runner
from command_handlers import CommandHandlers
from settings import (CAPACITY_FALLBACKS, CAPACITY_RESUME_MODELS, ConfigError, MODEL_RE,
                      OFFICIAL_MODELS, is_retired_model, resolve_model, Settings)
from state_store import Store, atomic_json
from telegram_api import TelegramAPI, TelegramError, chunks_utf16
from telegram_formatting import html_chunks, plain_html_text, render_markdown_html

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
    "location": "Google 拒绝当前地区的 API 调用：请检查服务器出口和 Google 账号所属地区是否受支持；刷新模型或重新授权不能保证解决地区限制。",
    "auth": "需要重新授权：请在服务器运行 agy auth login，完成 Google 登录后再试。",
    "quota": "账号额度或限流：请稍后再试，或检查当前 Google 账号额度。",
    "model": "模型不可用或参数不兼容：发送 /model refresh 重新检测可用模型。",
    "entitlement": "当前账号或套餐没有此模型权限：请选择已可用模型；更换账号或套餐后可用 /model refresh 重新检测。",
    "permission": "工具审批被拒绝：请检查 /status 中的自动审批设置与 agy 权限规则；需要编辑时使用 /mode code。",
    "filesystem_readonly": "文件系统只读：自动审批不能解除 systemd 的目录限制。请通过安装器 --write-paths 显式开放需要修改的应用目录，见 README。",
    "filesystem_permission": "系统权限不足：请核对目标文件权限、ACL 与服务 CapabilityBoundingSet；开启 agy 自动审批不能补足操作系统权限。",
    "network": "服务器网络异常：请检查 DNS、代理和到 Google 的连接。",
    "process": "agy 无法启动：请检查服务器上的 agy 安装与运行权限。",
}
OUTCOME_HELP = {
    "timed_out": "任务超时：任务可能仍留下部分修改，请先检查工作目录再决定是否重试。",
    "output_limit": "输出超过上限：请缩小任务范围，或查看工作目录中的实际变更。",
    "invalid": "结果格式异常：任务可能已部分执行，请核对工作目录和 /last 记录。",
    "no_text": "未收到最终文字回复：请先核对工作目录和 /last 记录。",
}
MODEL_PROBE_PROMPT = "Reply with exactly: AGY_MODEL_CHECK_OK. Do not use tools, read files, modify files, or make network requests."
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ALBUM_FILES = 10
MAX_ALBUM_BYTES = 30 * 1024 * 1024
ALBUM_SETTLE_SECONDS = 1.5
SESSION_TOKEN_REMINDER_THRESHOLD = 100_000
TYPING_HEARTBEAT_SECONDS = 4
PROGRESS_UPDATE_SECONDS = 5
RESTART_DELAY_SECONDS = 0.8
CORRECTION_TTL_SECONDS = 300
CAPACITY_RESUME_PROMPT = (
    "The immediately preceding task was interrupted by a temporary upstream capacity error. "
    "Continue the same task from this conversation. Review the completed steps first; do not repeat "
    "commands, edits, or other actions that have already completed. Perform only work still needed, "
    "then provide the final answer to the original user request."
)
SAFE_DOCUMENT_SUFFIXES = frozenset({
    ".txt", ".log", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".ps1", ".html", ".css",
    ".sql", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb", ".xml", ".csv",
})
LOG_SUFFIXES = frozenset({".log"})
CODE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".ps1", ".html", ".css",
    ".sql", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb",
})
FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
# Every official agy catalog entry already contains its reasoning profile in
# the model identifier.  Passing a second --effort argument makes the CLI
# reject several providers.  Flash variants can still use the same friendly
# picker: it switches the *model* suffix instead of passing --effort.
OFFICIAL_MODEL_IDS = frozenset(model_id for _, model_id, _ in OFFICIAL_MODELS)
CATALOG_VARIANT_RE = re.compile(r"^((?:gemini|claude)-[A-Za-z0-9.-]+)-(low|medium|high|max)$")


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




@dataclass
class ControlCard:
    chat: int
    text: str = ""
    markup: dict = field(default_factory=lambda: {"inline_keyboard": []})
    message_id: int | None = None
    revision: int = 0
    queued: bool = False
    closed: bool = False
    uncertain: bool = False


@dataclass
class ResetConfirmation:
    token: str
    context: tuple
    card: ControlCard
    expires: float
    timer: asyncio.TimerHandle | None = None


def control_html(text: str) -> str:
    """Style control text without interpreting arbitrary Markdown or HTML."""
    lines = [line for line in text.splitlines() if not re.fullmatch(r"━+", line)]
    if not lines:
        return ""
    def inline(value: str) -> str:
        parts = re.split(r"(`[^`\n]+`)", value)
        return "".join("<code>" + html.escape(part[1:-1]) + "</code>"
                       if part.startswith("`") and part.endswith("`") and len(part) > 2
                       else html.escape(part) for part in parts)
    title = "".join(part if part.startswith("<code>") else "<b>" + part + "</b>"
                    for part in re.split(r"(<code>.*?</code>)", inline(lines[0])) if part)
    body = "\n".join(inline(line) for line in lines[1:]).strip()
    return title + ("\n\n" + body if body else "")


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
    result_started: bool = False
    progress_task: asyncio.Task | None = None
    progress_expanded: bool = False
    progress_requested: asyncio.Event = field(default_factory=asyncio.Event)
    activity: str = "正在准备任务"
    phase: str = "preparing"
    pending_correction: str = ""
    correction_token: str = ""
    correction_expires: float = 0.0
    correction_card: ControlCard | None = None
    correction_timer: asyncio.TimerHandle | None = None
    attachment_dir: Path | None = None
    attachments: list[Path] = field(default_factory=list)
    attachment_feedback: str = ""
    execution_steps: list[str] = field(default_factory=list)
    tool_started: bool = False
    history_title: str = ""
    original_prompt: str = ""
    has_attachment: bool = False
    typing_task: asyncio.Task | None = None
    steer_task: asyncio.Task | None = None
    steered: bool = False
    steer_cancelled: bool = False
    reset_after: bool = False


def attachment_kind(name: str, is_photo: bool) -> str:
    if is_photo:
        return "图片"
    suffix = Path(name).suffix.lower()
    if suffix in LOG_SUFFIXES:
        return "日志文件"
    if suffix in CODE_SUFFIXES:
        return "代码文件"
    if suffix in {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".csv"}:
        return "文本文件"
    return "文件"


def attachment_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def task_title(text: str, fallback: str = "未命名任务") -> str:
    return " ".join(text.split())[:160] or fallback


def record_time_label(stamp: object, now: float | None = None) -> str:
    if type(stamp) not in {int, float} or not 0 < stamp <= 253402300799 or not math.isfinite(stamp):
        return "时间未知"
    try:
        value = datetime.fromtimestamp(stamp)
        today = datetime.fromtimestamp(time.time() if now is None else now).date()
        day = ("今天" if value.date() == today else
               "昨天" if value.date() == today - timedelta(days=1) else value.strftime("%m-%d "))
        return day + value.strftime("%H:%M")
    except (OSError, ValueError, OverflowError):
        return "时间未知"


def history_model_label(model: str) -> str:
    match = re.match(r"claude-(sonnet|opus)-(\d+)-(\d+)", model)
    if match:
        return f"{match[1].title()} {match[2]}.{match[3]}"
    match = re.match(r"gemini-(\d+(?:\.\d+)?)-(flash|pro|argon)", model)
    if match:
        return f"{match[2].title()} {match[1]}"
    return model[:32] or "默认模型"


def correction_prompt(job: Job, correction: str) -> str:
    return ("这是对刚才已取消任务的修正。请以“最新修正”为准，不要沿用已取消任务的未完成结论。\n\n"
            f"原任务：\n{job.original_prompt}\n\n最新修正：\n{correction}")


def history_summary(records: list[dict]) -> tuple[str, dict | None]:
    if not records:
        return "📚 任务历史\n━━━━━━━━━━━━━━━━━━━━\nℹ️ 暂无已完成的任务记录。", None
    lines = ["📚 任务历史（最近 10 条）", "时间按服务器本地时区 " + time.strftime("%z"), "━━━━━━━━━━━━━━━━━━━━"]
    keyboard: list[list[dict[str, str]]] = []
    for index, record in enumerate(records, 1):
        title = task_title(str(record.get("title", "未命名任务")))
        outcome = str(record.get("outcome", "error"))
        duration = record.get("duration_seconds")
        duration_text = f" ｜ ⏱️ {duration}s" if isinstance(duration, (int, float)) and duration > 0 else ""
        stamp = record_time_label(record.get("updated_at"))
        model = history_model_label(str(record.get("model") or ""))
        delivery = {"failed": "⚠️ 未送达", "failed_or_partial": "⚠️ 未完整送达",
                    "unknown": "⚠️ 投递未确认", "pending": "⚠️ 投递未确认",
                    "superseded_by_steer": "已修正替代"}.get(str(record.get("delivery") or ""), "")
        delivery_text = f" ｜ {delivery}" if delivery else ""
        lines.append(f"{index}. {LABELS.get(outcome, '结果需核对')} ｜ {stamp} ｜ {model}"
                     f"{duration_text}{delivery_text}\n   {title}")
        job_id = record.get("job_id")
        if isinstance(job_id, str) and re.fullmatch(r"[a-f0-9]{12}", job_id):
            label = f"{index}. {stamp} {title}"[:60]
            keyboard.append([{"text": label, "callback_data": f"history:{job_id}"}])
    lines.append("━━━━━━━━━━━━━━━━━━━━\n点击下方任务可查看完整结果。")
    return "\n".join(lines), {"inline_keyboard": keyboard} if keyboard else None


def supports_effort(model: str) -> bool:
    """Only future/custom models may receive the separate CLI --effort flag."""
    return not model or (model not in OFFICIAL_MODEL_IDS and not CATALOG_VARIANT_RE.fullmatch(model))


def variant_model_for_effort(model: str, effort: str,
                            model_ids=OFFICIAL_MODEL_IDS) -> str | None:
    """Return a concrete catalog slug, never an unsupported reasoning suffix."""
    match = CATALOG_VARIANT_RE.fullmatch(model)
    if match:
        candidate = f"{match.group(1)}-{effort}"
        return candidate if candidate in model_ids else None
    return None


def selected_variant_effort(model: str) -> str | None:
    match = CATALOG_VARIANT_RE.fullmatch(model)
    return match.group(2) if match else None


def has_effort_picker(model: str) -> bool:
    return bool(selected_variant_effort(model)) or supports_effort(model)


def effort_label(effort: str | None, model: str) -> str:
    chosen = selected_variant_effort(model) or effort
    if not supports_effort(model) and not chosen:
        return "模型内置"
    return {"low": "极速", "medium": "均衡", "high": "深度", "max": "最高"}.get(chosen or "", "默认")


def model_health_text(result: dict | None) -> str:
    if not result:
        return "⚪ 未检测"
    if result.get("outcome") == "success":
        return "✅ 已验证可用"
    category = str(result.get("category") or "unknown")
    labels = {
        "auth": "需要授权", "quota": "额度或限流", "model": "模型不可用",
        "entitlement": "账号或套餐无权限",
        "location": "地区不受支持",
        "capacity": "容量暂时不足", "network": "网络异常", "process": "agy 无法启动",
    }
    return f"❌ {labels.get(category, '调用失败')}"


def describe(record: dict) -> str:
    outcome = str(record.get("outcome", "error"))
    title = LABELS.get(outcome, "结果需核对")
    model = record.get("model", "")
    model_tag = f"｜{model}" if model else ""
    duration = record.get("duration_seconds")
    duration_tag = f"｜⏱️ {duration}s" if isinstance(duration, (int, float)) and duration > 0 else ""

    parts = [f"{title}{model_tag}{duration_tag}"]

    if record.get("text"):
        parts.append("💬 回复\n" + clean_model_reply(str(record["text"])))
    elif record.get("detail"):
        parts.append("ℹ️ 说明\n" + str(record["detail"]))

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
    if record.get("session_token_reminder"):
        session_tokens = record.get("session_tokens", 0)
        stats.append(
            f"• 💡 当前会话累计约 {session_tokens:,} Token；如回复变慢或上下文混乱，可发送 /new 开启新会话。"
        )
    if record.get("model_fallback_from"):
        stats.append(
            f"• 🔁 模型自动切换：{record['model_fallback_from']} → {model}（原模型暂时无容量）"
        )
    if record.get("metadata_warning"):
        parts.append("⚠️ " + str(record["metadata_warning"]))

    if stats:
        parts.append("📊 运行统计\n" + "\n".join(stats))

    help_text = CATEGORY_HELP.get(record.get("category"))
    outcome_help = OUTCOME_HELP.get(outcome)
    if help_text:
        parts.append(f"💡 建议：{help_text}")
    elif outcome_help:
        parts.append(f"💡 建议：{outcome_help}")

    if outcome not in {"success", "not_started", "running"}:
        parts.append("ℹ️ 没有自动重跑任务。请先核对工作目录；/last 只取回记录，不重新执行。")

    return "\n\n".join(parts)


def describe_html(record: dict) -> str:
    """Render an agy result as Telegram HTML without trusting model-provided tags."""
    parts = [question_html(record.get("title", ""), record.get("attachment_summary", ""))]
    outcome = str(record.get("outcome", "error"))
    if outcome != "success":
        parts.append(html.escape(LABELS.get(outcome, "⚠️ 结果需核对")))
    if record.get("text"):
        parts.append(render_markdown_html(str(record["text"])))
    elif record.get("detail"):
        parts.append(html.escape(str(record["detail"])))
    help_text = CATEGORY_HELP.get(record.get("category")) or OUTCOME_HELP.get(outcome)
    if help_text:
        parts.append("💡 " + html.escape(help_text))
    if record.get("metadata_warning"):
        parts.append("⚠️ " + html.escape(str(record["metadata_warning"])))
    if record.get("delivery") in {"failed", "failed_or_partial"}:
        parts.append("⚠️ 上次发送未完成；这里是已保存的结果，没有重新执行任务。")
    if record.get("model_fallback_from"):
        parts.append("⚠️ 模型自动切换（原模型暂时无容量）：" + html.escape(display_model(str(record["model_fallback_from"])))
                     + " → " + html.escape(display_model(str(record.get("model") or ""))))
    if outcome not in {"success", "not_started", "running"}:
        parts.append("ℹ️ 没有自动重跑任务；取消不会撤销已发生的修改。/last 只取回记录。")
    summary = str(record.get("execution_summary") or "").strip()
    if summary:
        parts.append("<blockquote expandable><b>执行过程</b>\n" + html.escape(summary) + "</blockquote>")
    parts.append(result_footer(record))
    if record.get("session_token_reminder"):
        tokens = record.get("session_tokens", 0)
        cumulative = f"约 {tokens:,} Token" if type(tokens) is int and tokens >= 0 else "较多上下文"
        parts.append(f"💡 当前会话累计{cumulative}；回复变慢或上下文混乱时，可发送 /new 开启新会话。")
    return "\n\n".join(part for part in parts if part)


def ui_excerpt(value: object, limit: int = 120, fallback: str = "未命名任务") -> str:
    value = " ".join(str(value or "").split()) or fallback
    return value if len(value) <= limit else value[:limit - 1] + "…"


def display_model(model: str, effort: str = "") -> str:
    """Use readable names in cards while leaving stored model identifiers intact."""
    match = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-(flash|pro|argon)(?:-(low|medium|high|max))?", model)
    if match:
        label = f"Gemini {match[1]} {match[2].title()}"
        effort = match[3] or effort
    else:
        match = re.fullmatch(r"claude-(sonnet|opus)-(\d+)-(\d+)(?:-(low|medium|high|max))?", model)
        if match:
            label = f"Claude {match[1].title()} {match[2]}.{match[3]}"
            effort = match[4] or effort
        else:
            label = model or "默认模型（由 agy 决定）"
    return ui_excerpt(label, 90) + (f" ({effort.capitalize()})" if effort else "")


def question_html(title: object, attachment: object = "") -> str:
    parts = ["❓ 提问：<b>" + html.escape(ui_excerpt(title, fallback="历史任务")) + "</b>"]
    if attachment:
        parts.append(html.escape(ui_excerpt(attachment, 180)))
    return "<blockquote>" + "\n".join(parts) + "</blockquote>"


def result_footer(record: dict) -> str:
    model = display_model(str(record.get("model") or ""), str(record.get("effort") or ""))
    duration = record.get("duration_seconds")
    elapsed = (f"{duration:g}s" if type(duration) in {int, float} and 0 <= duration <= 1e12
               and math.isfinite(duration) else "未提供")
    def count(key: str) -> str:
        value = record.get(key)
        return f"{value:,}" if type(value) is int and value >= 0 else "—"
    lines = [f"🤖 {html.escape(model)} · ⏱ {elapsed}",
             f"📊 输入 {count('input_tokens')} · 输出 {count('output_tokens')} · 共 {count('total_tokens')} tokens"]
    mode = record.get("mode")
    turns = record.get("num_turns")
    details = []
    if mode:
        details.append("规划模式" if mode == "plan" else "编辑模式" if mode == "accept-edits" else str(mode))
    if type(turns) is int and turns > 0:
        details.append(f"第 {turns} 轮对话")
    if details:
        lines.append(html.escape(" · ".join(details)))
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


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


def deployment_version(directory: Path | None = None) -> str:
    """Read only the running release's bounded commit marker, without Git."""
    path = (directory if directory is not None else Path(__file__).resolve().parent) / ".commit"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 42:
                return "未知（未提供有效版本标记）"
            value = os.read(fd, 43).decode("ascii").strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", value):
                return value.lower()[:12]
        finally:
            os.close(fd)
    except (OSError, UnicodeError, ValueError):
        pass
    return "未知（未提供有效版本标记）"


class Bridge(CommandHandlers):
    def __init__(self, settings: Settings, api: TelegramAPI, store: Store,
                 runner: Runner, ready_file: Path | None = None):
        self.settings, self.api, self.store, self.runner = settings, api, store, runner
        self.ready_file = ready_file
        self.polling_ready = False
        self._started_at = time.time()
        self._deployment_version = deployment_version()
        self.slot: Job | None = None
        self.stop = asyncio.Event()
        self.offset = store.offset()
        self._last_maintenance = 0.0
        self.replies: asyncio.Queue[tuple | ControlCard] = asyncio.Queue(maxsize=32)
        self.reply_worker: asyncio.Task | None = None
        self._next_id_reply = 0.0
        self.model_check_task: asyncio.Task | None = None
        self.model_check_user: int | None = None
        self.model_check_progress = ""
        self.model_check_cancelling = False
        self.model_check_id = ""
        self.model_check_started_at = 0.0
        self.model_check_card: ControlCard | None = None
        self.reset_confirmations: dict[int, ResetConfirmation] = {}
        self._pending_results: set[tuple] = set()
        self._recent_results: dict[tuple, float] = {}
        self._selector_fallbacks: dict[tuple, float] = {}
        self.pending_album: dict | None = None
        self.album_task: asyncio.Task | None = None
        self.restart_task: asyncio.Task | None = None
        self.steer_tasks: set[asyncio.Task] = set()
        self.callback_tasks: set[asyncio.Task] = set()
        self.waiting_jobs: deque[tuple[int, int, str]] = deque()
        self.queue_task: asyncio.Task | None = None

    def _answer_callback(self, callback_id: str, **kwargs) -> None:
        if len(self.callback_tasks) >= 64 or self.stop.is_set():
            return
        async def answer() -> None:
            try:
                await self.api.answer_callback_query(callback_id, **kwargs)
            except Exception as error:
                LOG.warning("callback_answer_failed type=%s", type(error).__name__)
        task = asyncio.create_task(answer())
        self.callback_tasks.add(task)
        task.add_done_callback(self.callback_tasks.discard)

    def _status_card(self, user: int, details: bool = False) -> tuple[str, dict]:
        keyboard = {"inline_keyboard": [[
            {"text": "⬅️ 返回概览" if details else "📋 详细信息",
             "callback_data": "status:summary" if details else "status:details"},
            {"text": "🔄 刷新状态", "callback_data": "status:details" if details else "status:summary"},
        ]]}
        if details:
            return control_html(self.store.redact(self._status_details(user))), keyboard
        lines = ["📋 当前状态"]
        job = self.slot
        current = self.store.get_model(user) or self.settings.model or ""
        if self.runner.blocked:
            lines.append("⚠️ 新任务已暂停，请检查服务器；已保存结果仍可取回。")
        if job is not None and job.user == user:
            stage = ("正在清理并交接修正任务" if job.steered and job.steer_task and not job.steer_task.done()
                     else "正在取消并清理" if job.cancel.is_set()
                     else {"preparing": "准备任务", "receiving": "接收附件", "starting": "启动模型",
                           "saving": "保存结果", "sending": "结果已完成，正在发送"}.get(job.phase, job.activity))
            lines.extend([f"当前任务：{task_title(job.history_title)}", f"阶段：{stage}",
                          f"已运行：{max(0, int(time.monotonic() - job.started_at))} 秒"])
            current = job.model
        elif self.model_check_task is not None and not self.model_check_task.done():
            stage = "正在取消并清理模型检测" if self.model_check_cancelling else self.model_check_progress
            lines.append(f"🔎 模型检测：{stage}")
            if self.model_check_started_at:
                lines.append(f"已运行：{max(0, int(time.monotonic() - self.model_check_started_at))} 秒")
        elif self.pending_album is not None:
            lines.append("正在接收你的相册" if self.pending_album["user"] == user else "工作目录正被其他用户占用")
        elif job is not None:
            lines.append("你当前没有任务；工作目录正被其他白名单用户占用。")
        elif not self.runner.blocked:
            lines.append("你当前没有任务，可以直接发送需求。")
        queued = sum(1 for item in self.waiting_jobs if item[0] == user)
        if queued:
            lines.append(f"等待队列：你的 {queued} 个任务；发送 /queue 查看或取消。")
        lines.append(f"模型：{self._model_label(current)}")
        lines.append(f"部署版本：{self._deployment_version}")
        lines.append("路径、权限和启动时间可点“详细信息”查看。")
        return self.store.redact("\n".join(lines)), keyboard

    def _status_details(self, user: int) -> str:
        current_model = self.store.get_model(user) or self.settings.model
        model_info = f" [模型：{current_model}]" if current_model else ""
        effort = self.store.get_effort(user)
        effort_info = f"\n⚡ 思考强度：{effort.capitalize()}" if effort else ""
        mode = self.store.get_mode(user)
        mode_name = "推演规划 (Plan)" if mode == "plan" else ("落地编辑 (Accept-Edits)" if mode == "accept-edits" else mode)
        mode_info = f"\n📋 执行模式：{mode_name or '跟随 agy 设置（不保证自动编辑）'}"
        permission_info = (
            f"\n🔐 工具自动审批：{'开启' if self.settings.skip_permissions else '关闭（无交互任务可能被拒绝）'}"
            f"\n📁 工作目录：{self.settings.workspace}"
            f"\n📝 额外可写目录（配置值）：{', '.join(map(str, self.settings.write_paths)) or '未配置'}"
            f"\n🖥️ 主机权限（配置值）：{'完整 Root 权限' if self.settings.host_access == 'full' else '受限目录模式'}"
            "\n💡 实际权限以主机与 systemd 当前设置为准。"
        )
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
        started = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(self._started_at))
        text += (f"\n🔖 部署版本：{self._deployment_version}"
                 f"\n🕒 服务启动：{started}（服务器时间）")
        text += conv_info + effort_info + mode_info + disk_info + permission_info
        if self.model_check_task is not None and not self.model_check_task.done():
            text += ("\n🔎 模型检测：正在取消并清理" if self.model_check_cancelling
                     else f"\n🔎 模型检测：{self.model_check_progress}")
        return text

    def _restart_pending(self) -> bool:
        return self.restart_task is not None and not self.restart_task.done()

    def _restart_busy(self) -> bool:
        return (self.slot is not None or self.pending_album is not None
                or (self.album_task is not None and not self.album_task.done())
                or (self.model_check_task is not None and not self.model_check_task.done())
                or any(not task.done() for task in self.steer_tasks))

    async def _do_restart(self) -> None:
        try:
            await asyncio.sleep(RESTART_DELAY_SECONDS)
            if self.stop.is_set():
                return
            if self._restart_busy():
                self.queue_reply(self.settings.owner_id, "⚠️ 任务或清理状态发生变化，已取消本次重启。请等待全部结束后重试。")
                return
            # Exit nonzero so Restart=on-failure cleans the service cgroup and
            # starts a fresh process. Never exit while an active job owns the slot.
            os._exit(75)
        except Exception as error:
            LOG.error("restart_exit_failed type=%s", type(error).__name__)
            self.queue_reply(self.settings.owner_id, "⚠️ 重新载入失败，请检查服务器后重试。")
        finally:
            self.restart_task = None

    def _model_health(self) -> dict:
        try:
            return self.store.get_model_health()
        except (OSError, ValueError):
            return {"checked_at": 0.0, "models": {}}

    def _model_is_known_unavailable(self, model: str) -> bool:
        if is_retired_model(model):
            return True
        result = self._model_health().get("models", {}).get(model)
        return (isinstance(result, dict) and result.get("outcome") != "success"
                and result.get("category") in {"model", "entitlement"})

    async def _refresh_model_health(self, chat_id: int, model: str | None = None) -> None:
        results: dict[str, dict] = {}
        try:
            if model is None:
                models = tuple(item for item in await self.runner.list_models()
                               if not is_retired_model(item))
                results = {item: {"outcome": "error", "category": "model", "checked_at": time.time()}
                           for item in OFFICIAL_MODEL_IDS if item not in models}
            else:
                models = (model,)
                results = dict(self._model_health().get("models", {}))
            for index, target in enumerate(models, 1):
                self.model_check_progress = f"{index}/{len(models)}：{target}"
                self._update_probe_card()
                try:
                    result = await asyncio.wait_for(
                        self.runner.run(MODEL_PROBE_PROMPT, asyncio.Event(), model=target), timeout=45,
                    )
                    results[target] = {"outcome": result.outcome if result.outcome in {
                        "success", "error", "timed_out", "invalid"
                    } else "error", "category": result.category or "unknown", "checked_at": time.time()}
                except asyncio.TimeoutError:
                    results[target] = {"outcome": "timed_out", "category": "network", "checked_at": time.time()}
                if self.runner.blocked:
                    raise RuntimeError("Model probe cleanup failed")
            self.store.set_model_health(results)
            available = sum(results[item]["outcome"] == "success" for item in models)
            heading = "当前模型检测完成" if model is not None else "模型可用性检测完成"
            icon = "✅" if available == len(models) and available > 0 else "⚠️"
            failures = [f"• {display_model(item)}：{model_health_text(results[item])}"
                        for item in models if results[item]["outcome"] != "success"]
            self._finish_probe_card(
                f"{icon} {heading}：{available}/{len(models)} 个 CLI 模型已验证可用。\n"
                + ("\n".join(failures[:12]) + "\n" if failures else "")
                + (f"另有 {len(failures) - 12} 个未通过，可在模型菜单查看状态。\n" if len(failures) > 12 else "")
                + "账号无权限或 CLI 不支持的选项隐藏，临时故障保留并标注。")
        except asyncio.CancelledError:
            if self.runner.blocked:
                self._finish_probe_card("⚠️ 模型检测清理未确认完成，已暂停新任务；请检查服务器。")
            else:
                self._finish_probe_card("🛑 模型检测已取消并完成清理，保留上次完整检测结果。")
            raise
        except Exception as error:
            LOG.warning("model_health_check_failed type=%s", type(error).__name__)
            self._finish_probe_card("⚠️ 模型目录或可用性检测未完成，保留上次检测结果；请检查 agy 版本和授权后重试。")
        finally:
            if self.model_check_task is asyncio.current_task():
                self._model_check_finished(self.model_check_task)

    def _model_check_finished(self, task: asyncio.Task) -> None:
        if self.model_check_task is task:
            if self.model_check_card is not None and not self.model_check_card.closed:
                self._finish_probe_card("🛑 模型检测已结束，保留上次完整检测结果。")
            self.model_check_task = None
            self.model_check_user = None
            self.model_check_progress = ""
            self.model_check_cancelling = False
            self.model_check_id = ""
            self.model_check_started_at = 0.0
            self.model_check_card = None

    def _update_probe_card(self) -> None:
        card = self.model_check_card
        if card is None or card.closed:
            return
        elapsed = max(0, int(time.monotonic() - self.model_check_started_at))
        state = "正在取消并清理" if self.model_check_cancelling else self.model_check_progress
        self.queue_card(card, f"🔎 模型可用性检测\n{state}\n⏱ 已运行 {elapsed}s\n"
                        "会消耗模型额度；检测提示要求不用工具、不读写工作目录。", {
                            "inline_keyboard": [[
                                {"text": "🔄 刷新进度", "callback_data": f"probe:progress:{self.model_check_id}"},
                                {"text": "🛑 取消检测", "callback_data": f"probe:cancel:{self.model_check_id}"},
                            ]]})

    def _finish_probe_card(self, text: str) -> None:
        if self.model_check_card is not None:
            self.model_check_card.closed = True
            self.queue_card(self.model_check_card, text, {"inline_keyboard": [[
                {"text": "⬅️ 返回模型", "callback_data": "selector:models"},
                {"text": "✖️ 关闭", "callback_data": "selector:close"},
            ]]})

    def _cancel_model_health(self) -> None:
        if (self.model_check_task is not None and not self.model_check_task.done()
                and not self.model_check_cancelling):
            self.model_check_cancelling = True
            self._update_probe_card()
            self.model_check_task.cancel()

    def _start_model_health_refresh(self, chat_id: int, model: str | None = None) -> None:
        if self.runner.blocked:
            self.queue_reply(chat_id, "⚠️ 新任务已暂停，请先检查服务器状态。")
            return
        if model is not None and (not MODEL_RE.fullmatch(model) or is_retired_model(model)):
            self.queue_reply(chat_id, "⚠️ 请先选择有效的具体模型，再检测当前模型。")
            return
        if self._restart_pending():
            self.queue_reply(chat_id, "⏳ 正在重新载入服务，请稍后再检测模型。")
            return
        if (self.slot is not None or self.pending_album is not None
                or (self.album_task is not None and not self.album_task.done())
                or any(not task.done() for task in self.steer_tasks)):
            self.queue_reply(chat_id, "⏳ 当前有任务运行中。请等待任务结束后再发送 /model refresh。")
            return
        if self.model_check_task and not self.model_check_task.done():
            self.queue_reply(chat_id, "⏳ 模型可用性检测正在进行，请稍候。")
            return
        self.model_check_user = chat_id
        self.model_check_cancelling = False
        self.model_check_progress = "正在准备检测当前模型" if model is not None else "正在读取模型目录"
        self.model_check_id = uuid.uuid4().hex[:12]
        self.model_check_started_at = time.monotonic()
        self.model_check_card = ControlCard(chat_id)
        self._update_probe_card()
        self.model_check_task = asyncio.create_task(self._refresh_model_health(chat_id, model))
        self.model_check_task.add_done_callback(self._model_check_finished)

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

    async def send_html(self, chat: int, text: str, reply_markup: dict | None = None,
                        message_id: int | None = None, delivery_job: Job | None = None) -> bool:
        chunks = html_chunks(self.store.redact(text))
        total = len(chunks)
        for index, chunk in enumerate(chunks, 1):
            suffix = f"\n\n📄 [第 {index}/{total} 页]" if total > 1 else ""
            markup = reply_markup if index == total else None
            replace = index == 1 and type(message_id) is int and message_id > 0
            try:
                if replace:
                    await self.api.edit(chat, message_id, chunk + suffix,
                                        reply_markup=markup or {"inline_keyboard": []}, parse_mode="HTML")
                elif markup is not None:
                    await self.api.send(chat, chunk + suffix, parse_mode="HTML", reply_markup=markup)
                else:
                    await self.api.send(chat, chunk + suffix, parse_mode="HTML")
            except TelegramError as error:
                LOG.warning("telegram_html_delivery_failed code=%s", error.code)
                # Only a definite rejection can be retried. Downgrade this page,
                # never replay pages that Telegram has already accepted.
                if error.code != 400:
                    return False
                plain = plain_html_text(chunk)
                if replace:
                    # A rejected edit is safe to downgrade once. An ambiguous
                    # network result must never duplicate the result as a send.
                    # Link destinations can make the plain fallback longer.
                    if len((plain + suffix).encode("utf-16-le")) // 2 > 4096:
                        return False
                    try:
                        await self.api.edit(chat, message_id, plain + suffix,
                                            reply_markup=markup or {"inline_keyboard": []})
                    except TelegramError:
                        return False
                elif not await self.send_text(chat, plain + suffix, reply_markup=markup):
                    return False
            if index == 1 and delivery_job is not None:
                delivery_job.result_started = True
        return True

    def _release_job(self, job: Job) -> None:
        self._forget_correction(job)
        handoff = job.steer_task
        if self.slot is job and not (handoff is not None and not handoff.done()):
            self.slot = None
            self._wake_queue()

    def _wake_queue(self) -> None:
        if (not self.waiting_jobs or self.stop.is_set() or self.runner.blocked
                or self.slot is not None or self.pending_album is not None
                or self._restart_pending() or any(not task.done() for task in self.steer_tasks)
                or (self.model_check_task is not None and not self.model_check_task.done())
                or (self.queue_task is not None and not self.queue_task.done())):
            return
        self.queue_task = asyncio.create_task(self._start_queued())

    async def _start_queued(self) -> None:
        while self.waiting_jobs and not self.stop.is_set() and not self.runner.blocked:
            if self.slot is not None or self.pending_album is not None or self._restart_pending():
                return
            user, chat, text = self.waiting_jobs.popleft()
            if not self._is_allowed(user):
                continue
            await self.handle({"message": {"from": {"id": user},
                "chat": {"id": chat, "type": "private"}, "text": text}}, from_queue=True)
            if self.slot is not None:
                return

    def queue_control(self, chat: int, text: str, reply_markup: dict | None = None) -> bool:
        return self.queue_reply(chat, control_html(text), reply_markup, html_mode=True)

    def queue_card(self, card: ControlCard, text: str, markup: dict | None = None) -> None:
        card.text, card.markup = text, markup if markup is not None else {"inline_keyboard": []}
        card.revision += 1
        if card.queued or self.stop.is_set():
            return
        try:
            self.replies.put_nowait(card)
        except asyncio.QueueFull:
            return
        card.queued = True
        if self.reply_worker is None or self.reply_worker.done():
            self.reply_worker = asyncio.create_task(self._send_replies())

    async def _send_card(self, card: ControlCard) -> None:
        if card.uncertain:
            card.queued = False
            return
        revision = card.revision
        text, markup = control_html(self.store.redact(card.text)), card.markup
        try:
            if card.message_id is None:
                result = await self.api.send(card.chat, text, reply_markup=markup, parse_mode="HTML")
                if isinstance(result, dict) and type(result.get("message_id")) is int:
                    card.message_id = result["message_id"]
            else:
                await self.api.edit(card.chat, card.message_id, text, reply_markup=markup, parse_mode="HTML")
        except TelegramError as error:
            # Never duplicate a card after an ambiguous network result.
            LOG.warning("control_card_delivery_failed code=%s", error.code)
            if card.message_id is None:
                card.uncertain = True
        finally:
            card.queued = False
            if revision != card.revision:
                self.queue_card(card, card.text, card.markup)

    def queue_reply(self, chat: int, text: str, reply_markup: dict | None = None,
                    html_mode: bool = False, *, result_key: tuple | None = None) -> bool:
        # Control replies must never hold up update ingestion or cancellation.
        if self.stop.is_set():
            return False
        now = time.monotonic()
        if result_key is not None:
            if result_key in self._pending_results or now < self._recent_results.get(result_key, 0):
                return False
        try:
            self.replies.put_nowait((chat, text, reply_markup, html_mode, result_key))
        except asyncio.QueueFull:
            return False
        if result_key is not None:
            self._pending_results.add(result_key)
        if self.reply_worker is None or self.reply_worker.done():
            self.reply_worker = asyncio.create_task(self._send_replies())
        return True

    async def _send_replies(self) -> None:
        while not self.replies.empty():
            item = self.replies.get_nowait()
            if isinstance(item, ControlCard):
                try:
                    await self._send_card(item)
                finally:
                    self.replies.task_done()
                continue
            result_key = None
            if len(item) == 5:
                chat, text, reply_markup, html_mode, result_key = item
            elif len(item) == 4:
                chat, text, reply_markup, html_mode = item
            else:
                chat, text = item
                reply_markup = None
                html_mode = False
            delivered = False
            try:
                if html_mode:
                    delivered = await self.send_html(chat, text, reply_markup=reply_markup)
                else:
                    delivered = await self.send_text(chat, text, reply_markup=reply_markup)
            finally:
                if result_key is not None:
                    self._pending_results.discard(result_key)
                    if delivered:
                        self._recent_results = {key: until for key, until in self._recent_results.items()
                                                if until > time.monotonic()}
                        if len(self._recent_results) >= 128:
                            self._recent_results.pop(next(iter(self._recent_results)))
                        self._recent_results[result_key] = time.monotonic() + 2
                self.replies.task_done()

    async def _accept_and_work(self, job: Job, prompt: str) -> None:
        try:
            job.phase = "starting"
            job.activity = "正在启动模型"
            accept_msg = self._progress_text(job)
            cancel_markup = self._progress_buttons(job)
            try:
                accept_started = time.monotonic()
                sent = await self.api.send(job.chat, accept_msg, reply_markup=cancel_markup, parse_mode="HTML")
                LOG.info("telegram_task_accepted send_seconds=%.2f", time.monotonic() - accept_started)
                job.status_message_id = sent.get("message_id") if isinstance(sent, dict) else None
                accepted = True
            except TelegramError:
                accepted = False
            if not accepted or self.stop.is_set() or job.cancel.is_set():
                record = self.store.save(job.user, {
                    "job_id": job.job_id, "outcome": "not_started",
                    "detail": "确认消息未成功投递、任务已取消或服务正在停止；没有启动 agy。",
                    "delivery": "sent" if accepted else "failed",
                    "model": job.model,
                    "effort": job.effort,
                    "mode": job.mode,
                    "title": job.history_title,
                    "attachment_summary": job.attachment_feedback,
                })
                self.store.save_history(job.user, job.history_title, record)
                return
            await self._work(job, prompt)
        except Exception as error:
            LOG.error("job_accept_failed id=%s type=%s", job.job_id, type(error).__name__)
            self.queue_reply(job.chat, "任务准备失败，未启动或未确认结果。请检查服务器，不要直接重跑。")
        finally:
            self._cleanup_attachments(job)
            if job.reset_after:
                try:
                    self.store.reset_conversation(job.user)
                except (OSError, ValueError):
                    self.queue_reply(job.chat, "⚠️ 新对话未能建立，请检查服务器状态。")
            self._release_job(job)

    async def _work(self, job: Job, prompt: str) -> None:
        # A single worker owns the slot until the execution, cleanup and storage finish.
        try:
            job.phase = "running"
            job.progress_task = asyncio.create_task(self._refresh_progress(job))
            job.typing_task = asyncio.create_task(self._typing_heartbeat(job))
            result = await self._run_task(job, prompt)
            fallback_from = ""
            is_capacity_error = (
                result.outcome == "error"
                and (result.category == "capacity" or "no capacity available" in result.detail.lower())
            )
            # Keep 3.8 High selected. agy can resume its returned conversation
            # so a transient capacity error does not restart the original task.
            if (job.model in CAPACITY_RESUME_MODELS and not job.cancel.is_set()
                    and is_capacity_error and result.conversation_id):
                job.conversation_id = result.conversation_id
                job.activity = "3.8 High 暂时无容量，正在恢复同一任务"
                job.execution_steps.append("⚠️ 3.8 High 暂时无容量，正在恢复同一任务")
                del job.execution_steps[:-6]
                result = await self._run_task(job, CAPACITY_RESUME_PROMPT)
            fallback = CAPACITY_FALLBACKS.get(job.model)
            tool_has_run = job.tool_started or any("💻" in step or "🛠️" in step for step in job.execution_steps)
            if (fallback and not job.cancel.is_set() and result.outcome == "error"
                    and not tool_has_run
                    and (result.category == "capacity" or "no capacity available" in result.detail.lower())):
                # Only a pre-tool capacity rejection may be retried safely.
                # A completed tool step may have modified user state already.
                fallback_from = job.model
                job.model = fallback
                job.effort = ""
                job.activity = f"{fallback_from} 暂无容量，正在自动切换至 {fallback}"
                job.execution_steps.append(f"⚠️ 原模型暂无容量，已自动切换至 {fallback}")
                del job.execution_steps[:-6]
                self.store.set_model(job.user, fallback)
                self._mark_model_capacity_unavailable(fallback_from)
                result = await self._run_task(job, prompt)

            job.phase = "saving"
            # Persist the execution evidence before optional accounting/context
            # writes: their failure must not leave only a stale running record.
            record = self.store.save(
                job.user, result.to_dict() | {
                    "job_id": job.job_id, "delivery": "pending",
                    "model": job.model or getattr(result, "model", ""),
                    "effort": job.effort or getattr(result, "effort", ""),
                    "mode": job.mode or getattr(result, "mode", ""),
                    "title": job.history_title,
                    "model_fallback_from": fallback_from,
                    "attachment_summary": job.attachment_feedback,
                    "execution_summary": "\n".join(job.execution_steps[-6:]),
                }
            )
            self.store.save_history(job.user, job.history_title, record)
            warnings = []
            if result.outcome == "success" and getattr(result, "conversation_id", None):
                try:
                    previous = self.store.get_conversation(job.user)
                    previous_tokens = 0
                    if previous and previous.get("conversation_id") == result.conversation_id:
                        previous_tokens = max(0, int(previous.get("session_tokens", 0) or 0))
                    session_tokens = previous_tokens + max(0, int(result.total_tokens or 0))
                    self.store.set_conversation(job.user, result.conversation_id,
                                                result.num_turns, session_tokens)
                    record.update(session_tokens=session_tokens, session_token_reminder=(
                        previous_tokens < SESSION_TOKEN_REMINDER_THRESHOLD <= session_tokens))
                except (OSError, ValueError, TypeError) as error:
                    LOG.warning("conversation_save_failed type=%s", type(error).__name__)
                    warnings.append("任务结果已保存，但会话记录更新失败；请检查服务器。")
            if result.total_tokens > 0:
                try:
                    self.store.record_usage(job.user, result.input_tokens,
                                            result.output_tokens, result.thinking_tokens)
                except (OSError, ValueError, TypeError) as error:
                    LOG.warning("usage_save_failed type=%s", type(error).__name__)
                    warnings.append("任务结果已保存，但累计用量更新失败，本次统计可能缺失。")
            if warnings:
                record["metadata_warning"] = " ".join(warnings)
            job.phase = "sending"
            LOG.info("job_finished id=%s outcome=%s", job.job_id, result.outcome)
            if job.progress_task:
                job.progress_task.cancel()
                await asyncio.gather(job.progress_task, return_exceptions=True)
            if job.steered:
                await self._finish_progress(job, result.outcome)
            delivered = True if job.steered else await self.send_html(
                job.chat, describe_html(record), reply_markup=self._completion_buttons(job),
                message_id=job.status_message_id, delivery_job=job,
            )
            final_record = self.store.save(job.user, record | {
                "delivery": "superseded_by_steer" if job.steered else ("sent" if delivered else "failed_or_partial")
            })
            self.store.save_history(job.user, job.history_title, final_record)
            card_updated = True if job.steered or delivered else False
            if not card_updated and not job.result_started:
                card_updated = await self._finish_progress(job, result.outcome, final_record["delivery"])
            if not delivered and not card_updated:
                await self.send_text(
                    job.chat,
                    f"{LABELS.get(result.outcome, '任务已结束')}\n"
                    "⚠️ 结果发送失败或不完整，已保存。点击“查看结果”或发送 /last 取回，不会重新执行任务。",
                    reply_markup=self._completion_buttons(job),
                )
        except asyncio.CancelledError:
            job.cancel.set()
            # Runner normally consumes cancellation only after cleaning its group.
            LOG.warning("worker_cancelled id=%s", job.job_id)
            raise
        except Exception as error:
            # Never print raw exception text: it may contain request URLs or secrets.
            LOG.error("worker_failed id=%s type=%s", job.job_id, type(error).__name__)
            await self.send_text(
                job.chat,
                f"任务 {job.job_id} 的结果保存或内部处理异常。"
                "请检查服务器和已有结果。不要直接重复原任务。",
            )
        finally:
            if job.progress_task:
                job.progress_task.cancel()
                await asyncio.gather(job.progress_task, return_exceptions=True)
            if job.typing_task:
                job.typing_task.cancel()
                await asyncio.gather(job.typing_task, return_exceptions=True)
            self._release_job(job)

    async def _run_task(self, job: Job, prompt: str) -> Result:
        async def progress(activity: str) -> None:
            if activity.startswith("正在调用工具") or "💻" in activity or "🛠️" in activity:
                job.tool_started = True
            if activity.startswith(("✅", "⚠️")):
                if not job.execution_steps or job.execution_steps[-1] != activity:
                    job.execution_steps.append(activity)
                    del job.execution_steps[:-6]
                job.activity = "正在继续处理任务"
            else:
                job.activity = activity
        return await self.runner.run(
            prompt, job.cancel, model=job.model or None,
            conversation_id=job.conversation_id or None,
            effort=job.effort or None, mode=job.mode or None, progress=progress,
        )

    def _mark_model_capacity_unavailable(self, model: str) -> None:
        health = self._model_health()
        models = dict(health.get("models", {}))
        models[model] = {"outcome": "error", "category": "capacity",
                         "checked_at": models.get(model, {}).get("checked_at", 0.0)}
        self.store.set_model_health(models)

    async def _typing_heartbeat(self, job: Job) -> None:
        """Keep Telegram's native typing indicator alive while a task is running."""
        # Do not add an extra Telegram API call for quick tasks.  The accepted
        # status card already acknowledges those; the native indicator starts
        # only when work is genuinely taking a while.
        await asyncio.sleep(TYPING_HEARTBEAT_SECONDS)
        while not self.stop.is_set() and not job.cancel.is_set() and self.slot is job:
            try:
                await self.api.call("sendChatAction", chat_id=job.chat, action="typing")
            except (TelegramError, AttributeError):
                return
            await asyncio.sleep(TYPING_HEARTBEAT_SECONDS)

    def _progress_text(self, job: Job) -> str:
        elapsed = max(0, int(time.monotonic() - job.started_at))
        state = "🛑 正在取消并清理" if job.cancel.is_set() else "⏳ 任务执行中"
        activity = "正在清理资源" if job.cancel.is_set() else ui_excerpt(job.activity, 90)
        trace = [ui_excerpt(step, 160) for step in job.execution_steps[-6:]]
        if not job.progress_expanded:
            trace = trace[-1:]
        if trace:
            # A repeatedly edited native expandable quote loses the client's
            # reading state. Our buttons own expansion; this quote never folds.
            trace_block = ("<blockquote><b>执行过程</b>\n"
                           + html.escape("\n".join(trace)) + "</blockquote>")
        else:
            trace_block = "<blockquote><b>执行过程</b>\n等待首个步骤…</blockquote>"
        text = (question_html(job.history_title or job.original_prompt, job.attachment_feedback)
                + f"\n\n<b>{state}</b>\n"
                + f"阶段：{html.escape(activity)}\n"
                + f"🤖 {html.escape(display_model(job.model, job.effort))} · ⏱ 已运行：{elapsed}s\n\n"
                + trace_block
                + ("\n📖 阅读中：内容保持不动，任务继续执行；点“刷新过程”查看最新进度。"
                   if job.progress_expanded else ""))
        return self.store.redact(text)

    def _progress_buttons(self, job: Job) -> dict:
        return {"inline_keyboard": [
            [{"text": "📥 收起过程" if job.progress_expanded else "📋 展开过程",
              "callback_data": f"progress:{'collapse' if job.progress_expanded else 'expand'}:{job.job_id}"},
             {"text": "🔄 刷新过程" if job.progress_expanded else "🔄 刷新进度",
              "callback_data": f"progress:refresh:{job.job_id}"}],
            [{"text": "🛑 取消任务", "callback_data": f"cancel:{job.job_id}"}],
        ]}

    async def _refresh_progress(self, job: Job) -> None:
        delay = PROGRESS_UPDATE_SECONDS
        retrying = False
        while not self.stop.is_set() and not job.cancel.is_set():
            if retrying:
                # A manual click must not bypass Telegram's rate-limit delay.
                await asyncio.sleep(delay)
                retrying = False
                requested = job.progress_requested.is_set()
                job.progress_requested.clear()
            else:
                waiter = asyncio.create_task(job.progress_requested.wait())
                try:
                    # Explicitly own the waiter. Some older Python wait_for
                    # implementations can lose cancellation when the inner
                    # event completes in the same loop turn.
                    done, _ = await asyncio.wait({waiter}, timeout=None if job.progress_expanded else delay)
                    requested = bool(done)
                    if requested:
                        job.progress_requested.clear()
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            if self.stop.is_set() or job.cancel.is_set():
                return
            if job.phase in {"saving", "sending"}:
                return
            if job.progress_expanded and not requested:
                continue
            if job.status_message_id is None or self.slot is not job:
                continue
            try:
                # All automatic and requested edits have one writer. A button
                # never races a pending HTTP edit, or the final result below.
                edit = asyncio.create_task(self.api.edit(job.chat, job.status_message_id,
                                                        self._progress_text(job), self._progress_buttons(job),
                                                        parse_mode="HTML"))
                try:
                    await asyncio.shield(edit)
                except asyncio.CancelledError:
                    # A thread-backed HTTP request must finish before the final
                    # card edit, otherwise an old request can overwrite completion.
                    await asyncio.gather(edit, return_exceptions=True)
                    raise
                delay = PROGRESS_UPDATE_SECONDS
            except TelegramError as error:
                if error.not_modified:
                    delay = PROGRESS_UPDATE_SECONDS
                    continue
                if error.code == 429:
                    delay = max(PROGRESS_UPDATE_SECONDS, error.retry_after)
                elif error.code == 0 or 500 <= error.code < 600:
                    delay = min(30, delay * 2)
                else:
                    return
                retrying = True
                if requested:
                    job.progress_requested.set()
            except AttributeError:
                return

    def _completion_buttons(self, job: Job) -> dict:
        return self._result_buttons(job.job_id)

    def _result_buttons(self, job_id: str) -> dict:
        return {"inline_keyboard": [
            [{"text": "📖 查看结果", "callback_data": f"history:{job_id}"},
             {"text": "📚 最近任务", "callback_data": "tasks:history"}],
            [{"text": "🧠 新建对话", "callback_data": "tasks:new"}],
        ]}

    async def _finish_progress(self, job: Job, outcome: str, delivery: str = "pending") -> bool:
        if job.status_message_id is None:
            return False
        try:
            delivery_text = {
                "pending": "结果已保存，正在尝试发送；也可点击下方按钮取回。",
                "sent": "结果已发送。可通过下方按钮查看结果、最近任务或新建对话。",
                "failed_or_partial": "⚠️ 结果发送失败或不完整，已保存。点击“查看结果”或发送 /last 取回，不会重新执行任务。",
            }.get(delivery, "结果已保存，可点击下方按钮查看。")
            completion = (
                "⚡ 已停止当前任务，将按你的修正重新开始。"
                if job.steered else
                f"{LABELS.get(outcome, '任务已结束')}\n\n{delivery_text}"
            )
            await self.api.edit(job.chat, job.status_message_id, completion,
                                {"inline_keyboard": []} if job.steered else self._completion_buttons(job))
            return True
        except (TelegramError, AttributeError):
            return False

    async def _restart_with_correction(self, job: Job, correction: str) -> None:
        """Wait for the old process to clean up, then start the corrected text task."""
        next_job = None
        try:
            if job.worker:
                await asyncio.gather(job.worker, return_exceptions=True)
            if self.stop.is_set() or self.runner.blocked:
                self.queue_reply(job.chat, "⚠️ 原任务已停止，但服务当前不能安全重启修正任务。请检查 /status 后重试。")
                return
            if job.steer_cancelled:
                self.queue_reply(job.chat, "🛑 已取消修正任务，没有重新执行。")
                return
            if self.slot is not job:
                self.queue_reply(job.chat, "⚠️ 任务位置已变化，未启动修正任务；请检查 /status。")
                return
            prompt = correction_prompt(job, correction)
            if len(prompt) > self.settings.max_prompt:
                self.queue_reply(job.chat, "⚠️ 原任务和修正合并后过长；请重新发送一条精简后的完整任务。")
                return
            next_job = Job(
                job.user, job.chat, model=job.model,
                conversation_id="" if job.reset_after else job.conversation_id,
                effort=job.effort, mode=job.mode, history_title=task_title(correction, "修正任务"),
                original_prompt=prompt, reset_after=job.reset_after,
            )
            self.slot = next_job
            self.store.maintain()
            self.store.save(job.user, {
                "job_id": next_job.job_id, "outcome": "running", "delivery": "pending",
                "title": next_job.history_title,
                "detail": "正在按用户修正重新执行；服务中断时不会自动重试。",
                "model": next_job.model, "effort": next_job.effort, "mode": next_job.mode,
            })
            next_job.worker = asyncio.create_task(self._accept_and_work(next_job, prompt))
        except Exception:
            if next_job is not None and self.slot is next_job:
                self.slot = None
            LOG.error("job_steer_failed id=%s", job.job_id)
            self.queue_reply(job.chat, "⚠️ 修正任务准备失败，未自动重试。请检查服务器后重新发送完整任务。")
        finally:
            if self.slot is job:
                self.slot = None

    async def _receive_attachment(self, job: Job, attachment: dict, is_photo: bool) -> tuple[Path, str]:
        """Fetch a whitelisted Telegram attachment into this job's private workspace."""
        size = attachment.get("file_size")
        file_id = attachment.get("file_id")
        if type(size) is not int or not 0 < size <= MAX_ATTACHMENT_BYTES or not isinstance(file_id, str) or not FILE_ID_RE.fullmatch(file_id):
            raise ValueError("invalid attachment metadata")
        original = "screenshot.jpg" if is_photo else str(attachment.get("file_name") or "")
        suffix = ".jpg" if is_photo else Path(original).suffix.lower()
        if not is_photo and suffix not in SAFE_DOCUMENT_SUFFIXES:
            raise ValueError("unsupported document")
        # User-facing names preserve Chinese; paths remain private ASCII-only names.
        display_name = re.split(r"[/\\]", original)[-1]
        display_name = " ".join("".join(
            char for char in self.store.redact(display_name)
            if not unicodedata.category(char).startswith("C")
        ).split())[:160]
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original).name)[:96]
        if not name or name in {".", ".."}:
            name = "screenshot.jpg" if is_photo else "attachment" + suffix
        base = self.settings.workspace / ".agy-telegram-inputs"
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            raise ValueError("unsafe attachment directory")
        base.mkdir(mode=0o700, exist_ok=True)
        directory = base / job.job_id
        if job.attachment_dir is None:
            directory.mkdir(mode=0o700)
            job.attachment_dir = directory
        destination = directory / name
        number = len(job.attachments) + 1
        while destination.exists():
            destination = directory / f"{Path(name).stem}-{number}{suffix}"
            number += 1
        try:
            info = await self.api.call("getFile", file_id=file_id)
            if not isinstance(info, dict) or not isinstance(info.get("file_path"), str):
                raise ValueError("missing file path")
            server_size = info.get("file_size", size)
            if type(server_size) is not int or not 0 < server_size <= MAX_ATTACHMENT_BYTES:
                raise ValueError("oversized file")
            if job.cancel.is_set() or self.stop.is_set():
                raise ValueError("attachment cancelled")
            downloaded = await self.api.download_file(
                info["file_path"], destination, MAX_ATTACHMENT_BYTES, cancel=job.cancel,
            )
            if not isinstance(downloaded, int) or not 0 < downloaded <= MAX_ATTACHMENT_BYTES:
                raise ValueError("invalid downloaded file")
        except (TelegramError, OSError, ValueError, AttributeError):
            destination.unlink(missing_ok=True)
            raise ValueError("attachment download failed") from None
        job.attachments.append(destination)
        if is_photo or not display_name:
            display_name = destination.name
        elif destination.name != name:
            display_name += f"（附件{len(job.attachments)}）"
        job.attachment_feedback = (
            f"📎 已收到：{attachment_kind(name, is_photo)}「{display_name}」，"
            f"{attachment_size(downloaded)}。本次任务会读取它。"
        )
        return destination, display_name

    def _cleanup_attachments(self, job: Job) -> None:
        for item in job.attachments:
            try:
                item.unlink(missing_ok=True)
            except OSError:
                LOG.warning("attachment_cleanup_failed id=%s", job.job_id)
        if job.attachment_dir is not None:
            try:
                job.attachment_dir.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                LOG.warning("attachment_directory_cleanup_failed id=%s", job.job_id)

    def _model_choices(self):
        """Add newly discovered slugs without rewriting saved user preferences."""
        catalog: dict[str, list[str]] = {}
        for label, model, _ in OFFICIAL_MODELS:
            catalog.setdefault(label.split(" (")[0], []).append(model)
        for label, variants in catalog.items():
            if label.startswith("Claude "):
                variants.sort(key=lambda value: (0 if value.endswith("-medium") else 1,
                                                 0 if value.endswith("-low") else 1))
        choices = [(label, tuple(variants)) for label, variants in catalog.items()]
        known = {model for _, variants in choices for model in variants}
        groups: dict[str, list[str]] = {}
        for model, result in self._model_health().get("models", {}).items():
            if (is_retired_model(model) or model in known or len(model) > 57
                    or result.get("category") in {"model", "entitlement"}):
                continue
            match = CATALOG_VARIANT_RE.fullmatch(model)
            group = match.group(1) if match else model
            groups.setdefault(group, []).append(model)
        for group, variants in groups.items():
            matching = next((i for i, (_, ids) in enumerate(choices)
                             if any(x.startswith(group + "-") for x in ids)), None)
            if matching is not None:
                label, ids = choices[matching]
                choices[matching] = (label, (*ids, *variants))
            else:
                choices.append((group, tuple(variants)))
        return choices

    def _variant_ids(self):
        return frozenset(model for _, variants in self._model_choices() for model in variants)

    def _model_label(self, model: str) -> str:
        if is_retired_model(model):
            return f"{model}（已移除，请重新选择）"
        for label, variants in self._model_choices():
            if model in variants:
                effort = selected_variant_effort(model)
                return f"{label} · {effort_label(effort, model)}" if effort else label
        return model or "默认（由 agy 决定）"

    def _model_picker(self, user: int) -> tuple[str, dict]:
        current = self.store.get_model(user) or self.settings.model or ""
        buttons = []
        notices = []
        health = self._model_health().get("models", {})
        for label, variants in self._model_choices():
            usable = [x for x in variants if not self._model_is_known_unavailable(x)]
            if not usable:
                continue
            verified = [x for x in usable if health.get(x, {}).get("outcome") == "success"]
            target = (verified or usable)[0]
            failed = health.get(target, {}).get("outcome") not in {None, "success"}
            icon = ("⚡" if "Flash" in label else "🧠" if "Pro" in label else "✍️" if "Sonnet" in label
                    else "💎" if "Opus" in label else "✨" if "Argon" in label else "🤖")
            buttons.append({"text": ("✓ " if current in variants else "") + ("⚠️ " if failed else icon + " ") + label,
                            "callback_data": f"model:{target}"})
            if failed:
                notices.append(f"{label}：{model_health_text(health[target])}")
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([{"text": "🔎 检测当前模型", "callback_data": "probe:current"},
                     {"text": "🔎 检测全部", "callback_data": "probe:all"}])
        rows.append([{"text": "↩️ 恢复默认", "callback_data": "model:default"},
                     {"text": "✖️ 关闭", "callback_data": "selector:close"}])
        text = f"选择模型\n当前：{self._model_label(current)}"
        stamp = health.get(current, {}).get("checked_at", 0)
        if type(stamp) in {int, float} and math.isfinite(stamp) and stamp > 0:
            age = max(0, int(time.time() - stamp))
            checked = ("刚刚" if age < 60 else f"{age // 60} 分钟前" if age < 3600
                       else f"{age // 3600} 小时前" if age < 86400 else record_time_label(stamp))
        else:
            checked = "未检测"
        text += f"\n当前模型上次检测：{checked}\n检测会消耗模型额度，不更改选定模型。"
        if notices:
            text += "\n\n上次检测（不代表套餐无权限）：\n" + "\n".join(notices)
        return text, {"inline_keyboard": rows}

    def _effort_picker(self, user: int, model_label: str, *, model: str | None = None) -> tuple[str, dict]:
        current = self.store.get_model(user) or self.settings.model or ""
        model = current if model is None else model
        curr = selected_variant_effort(current) or self.store.get_effort(user)
        buttons = []
        for effort in ("low", "medium", "high", "max"):
            if effort == "max" and not selected_variant_effort(model):
                continue
            variant = variant_model_for_effort(model, effort, self._variant_ids())
            if selected_variant_effort(model) and variant is None:
                continue
            if variant and self._model_is_known_unavailable(variant):
                continue
            selected = current == variant if variant else curr == effort and current == model
            icon = {"low": "⚡", "medium": "⚖️", "high": "🔍", "max": "🧠"}[effort]
            buttons.append({"text": ("✓ " if selected else "") + icon + " " + effort_label(effort, ""),
                            "callback_data": f"choose:{variant}" if variant else f"effort:{effort}"})
        keyboard = {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [[
            {"text": "⬅️ 返回模型", "callback_data": "selector:models"},
            {"text": "✖️ 关闭", "callback_data": "selector:close"},
        ]]}
        label = next((label for label, variants in self._model_choices() if model in variants), model_label)
        return f"{label}\n选择思考强度后生效", keyboard

    def _show_effort_picker(self, chat_id: int, user: int, model_label: str) -> None:
        text, keyboard = self._effort_picker(user, model_label)
        self.queue_reply(chat_id, text, reply_markup=keyboard)

    async def _collapse_selector(self, chat_id: int, message: dict, text: str,
                                 reply_markup: dict | None = None, *, html_mode: bool = False) -> bool:
        message_id = message.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            return False
        try:
            await self.api.edit(chat_id, message_id, text,
                                reply_markup=reply_markup if reply_markup is not None else {"inline_keyboard": []},
                                **({"parse_mode": "HTML"} if html_mode else {}))
            return True
        except TelegramError as error:
            if error.not_modified:
                return True
        except AttributeError:
            pass
        key = (chat_id, message_id, text, repr(reply_markup))
        now = time.monotonic()
        if now < self._selector_fallbacks.get(key, 0):
            return True
        self._selector_fallbacks = {key: until for key, until in self._selector_fallbacks.items() if until > now}
        if len(self._selector_fallbacks) >= 128:
            self._selector_fallbacks.pop(next(iter(self._selector_fallbacks)))
        self._selector_fallbacks[key] = now + 30
        return False

    def _reset_context(self, user: int) -> tuple:
        conversation = self.store.get_conversation(user) or {}
        job = self.slot if self.slot is not None and self.slot.user == user else None
        return (conversation.get("conversation_id"), conversation.get("num_turns"), job.job_id if job else None)

    def _reset_conversation(self, user: int) -> str:
        if self.slot is not None and self.slot.user == user:
            self.slot.reset_after = True
            return "🧠 已安排新对话：当前任务结束后清空上下文，下一条消息将开启全新对话。"
        self.store.reset_conversation(user)
        return "🧠 对话记忆已重置！\n已清空当前上下文，下一条消息将开启全新对话。\n历史结果与累计用量仍保留。"

    def _close_reset_confirmation(self, user: int, text: str) -> None:
        pending = self.reset_confirmations.pop(user, None)
        if pending is not None:
            if pending.timer is not None:
                pending.timer.cancel()
            pending.card.closed = True
            self.queue_card(pending.card, text)

    def _offer_reset_confirmation(self, user: int) -> None:
        previous = self.reset_confirmations.get(user)
        card = previous.card if previous else ControlCard(user)
        if previous and previous.timer:
            previous.timer.cancel()
        token = uuid.uuid4().hex[:12]
        pending = ResetConfirmation(token, self._reset_context(user), card, time.monotonic() + 90)
        pending.timer = asyncio.get_running_loop().call_later(
            90, self._close_reset_confirmation, user, "⌛ 新建对话确认已过期，当前对话未改变。")
        self.reset_confirmations[user] = pending
        self.queue_card(card, "🧠 确认新建对话？\n"
                        "将清空当前对话记忆，历史结果和累计用量仍保留。\n"
                        "如果有任务运行，会在该任务结束后重置。\n90 秒内确认生效。", {
                            "inline_keyboard": [[
                                {"text": "🧠 确认新建", "callback_data": f"dialog:confirm:{token}"},
                                {"text": "↩️ 保留当前对话", "callback_data": f"dialog:dismiss:{token}"},
                            ]]})

    def _forget_correction(self, job: Job, reason: str = "ℹ️ 任务已结束，这条补充未执行。") -> None:
        if job.correction_timer is not None:
            job.correction_timer.cancel()
            job.correction_timer = None
        if job.correction_card is not None:
            job.correction_card.closed = True
            self.queue_card(job.correction_card, reason)
            job.correction_card = None
        job.pending_correction = ""
        job.correction_token = ""
        job.correction_expires = 0.0

    def _offer_correction(self, user: int, text: str) -> bool:
        job = self.slot
        if (job is None or job.user != user or job.has_attachment or job.attachments
                or job.cancel.is_set() or job.steered or job.steer_task is not None
                or job.phase in {"saving", "sending"} or not job.original_prompt):
            return False
        if not text.strip() or "\x00" in text or len(correction_prompt(job, text)) > self.settings.max_prompt:
            self.queue_reply(user, "⚠️ 补充与原任务合并后过长或格式无效。当前任务未改变，请精简补充内容。")
            return True
        job.pending_correction = text
        job.correction_token = uuid.uuid4().hex[:12]
        job.correction_expires = time.monotonic() + CORRECTION_TTL_SECONDS
        if job.correction_timer is not None:
            job.correction_timer.cancel()
        job.correction_timer = asyncio.get_running_loop().call_later(
            CORRECTION_TTL_SECONDS, self._forget_correction, job, "⌛ 补充已过期，当前任务未改变。")
        if job.correction_card is None:
            job.correction_card = ControlCard(user)
        prefix = f"correction:{{}}:{job.job_id}:{job.correction_token}"
        self.queue_card(job.correction_card,
                         "💬 收到一条待确认补充，当前任务仍在运行。\n"
                         "点击“作为修正”会先停止并清理当前任务，再按原需求和这段补充执行。\n"
                         f"补充预览：{self.store.redact(task_title(text))}\n"
                         "仅最新一条待确认补充有效，5 分钟后失效；不会自动执行。",
                         {"inline_keyboard": [[
                             {"text": "✏️ 作为修正", "callback_data": prefix.format("apply")},
                             {"text": "✖️ 忽略补充", "callback_data": prefix.format("dismiss")},
                         ]]})
        return True

    async def handle_callback(self, cq: dict) -> None:
        cq_id = str(cq.get("id") or "")
        sender = cq.get("from") or {}
        user = sender.get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id") or user
        data = str(cq.get("data") or "")

        if (type(user) is not int or not self._is_allowed(user) or sender.get("is_bot") is True
                or chat.get("type") != "private" or type(chat_id) is not int or chat_id != user):
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text="⚠️ 无操作权限", show_alert=True)
            return

        if data.startswith("progress:"):
            parts = data.split(":")
            job = self.slot
            valid = (len(parts) == 3 and parts[1] in {"expand", "collapse", "refresh"}
                     and job is not None and job.user == user and parts[2] == job.job_id
                     and type(message.get("message_id")) is int
                     and message["message_id"] == job.status_message_id
                     and job.phase in {"starting", "running"} and not job.cancel.is_set()
                     and not self.stop.is_set())
            notice = "任务已结束或这张进度卡已失效。"
            if valid:
                if parts[1] != "refresh":
                    job.progress_expanded = parts[1] == "expand"
                job.progress_requested.set()
                notice = {"expand": "正在展开过程，任务继续执行",
                          "collapse": "正在收起过程并恢复自动刷新",
                          "refresh": "正在刷新，保持当前展开状态"}[parts[1]]
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text=notice)
            return

        if data in {"status:summary", "status:details"}:
            text, keyboard = self._status_card(user, data == "status:details")
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id)
            if not await self._collapse_selector(chat_id, message, text, keyboard, html_mode=data == "status:details"):
                self.queue_reply(chat_id, text, reply_markup=keyboard, html_mode=data == "status:details")
            return

        if data.startswith("dialog:"):
            parts = data.split(":")
            pending = self.reset_confirmations.get(user)
            valid = (len(parts) == 3 and parts[1] in {"confirm", "dismiss"} and pending is not None
                     and parts[2] == pending.token and time.monotonic() < pending.expires)
            notice = "确认已失效，请重新点击“新建对话”。"
            if valid and parts[1] == "dismiss":
                self._close_reset_confirmation(user, "↩️ 已保留当前对话，没有清空记忆。")
                notice = "已保留当前对话"
            elif valid and pending.context != self._reset_context(user):
                self._close_reset_confirmation(user, "ℹ️ 当前对话已发生变化，请重新点击“新建对话”后确认。")
                notice = "对话已变化，请重新确认"
            elif valid:
                # Invalidate before any await so repeated taps cannot reset twice.
                self._close_reset_confirmation(user, self._reset_conversation(user))
                notice = "已确认新建对话"
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text=notice)
            return

        if data.startswith("probe:progress:"):
            valid = (self.model_check_task is not None and not self.model_check_task.done()
                     and data == f"probe:progress:{self.model_check_id}" and user == self.model_check_user)
            if valid:
                self._update_probe_card()
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text="进度已刷新" if valid else "检测已结束")
            return

        if data in {"probe:current", "probe:all"}:
            current = self.store.get_model(user) or self.settings.model or ""
            if data == "probe:current" and not current:
                self.queue_reply(chat_id, "💡 请先在 /model 选择具体模型，再检测当前模型；默认模型由 CLI 决定。")
            else:
                self._start_model_health_refresh(chat_id, current if data == "probe:current" else None)
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id)
            return

        if data.startswith("probe:cancel:"):
            valid = (bool(self.model_check_id) and self.model_check_task is not None and not self.model_check_task.done()
                     and data == f"probe:cancel:{self.model_check_id}"
                     and user in {self.model_check_user, self.settings.owner_id})
            if valid:
                self._cancel_model_health()
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text="已请求取消" if valid else "检测已结束、按钮已失效或无取消权限")
            return

        if data.startswith("correction:"):
            parts = data.split(":")
            job = self.slot
            valid = (len(parts) == 4 and parts[1] in {"apply", "dismiss"} and job is not None
                     and job.user == user and parts[2] == job.job_id and parts[3] == job.correction_token
                     and bool(job.pending_correction) and time.monotonic() < job.correction_expires
                     and not job.cancel.is_set() and not job.steered and job.steer_task is None
                     and job.phase not in {"saving", "sending"} and not self.runner.blocked
                     and not self._restart_pending())
            if not valid:
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text="补充已失效，请确认当前任务后重新发送。", show_alert=True)
                return
            correction = job.pending_correction
            text = "⚡ 已提交修正，正在等待清理和交接。" if parts[1] == "apply" else "已忽略这条补充，当前任务未改变。"
            self._forget_correction(job, text)
            # Reserve the correction through the ordinary /steer path before
            # awaiting any Telegram acknowledgement, so double taps cannot replay.
            if parts[1] == "apply":
                await self.handle({"message": {"chat": chat, "from": sender, "text": "/steer " + correction}})
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text="已提交修正" if parts[1] == "apply" else "已忽略补充")
            return

        if data.startswith("cancel:"):
            job = self.slot
            if job is not None and job.user == user and data == f"cancel:{job.job_id}":
                job.cancel.set()
                job.steer_cancelled = True
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text="🛑 已请求取消")
                self.queue_reply(chat_id, "🛑 已请求取消。正在清理任务进程；已经发生的修改不会自动撤销。")
            elif cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text="该任务已结束或不可取消")
            return

        if data.startswith("history:"):
            try:
                record = self.store.history_item(user, data[8:])
            except (OSError, ValueError) as error:
                LOG.warning("history_callback_read_failed type=%s", type(error).__name__)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(
                        cq_id, text="无法读取任务历史，请检查服务器状态。", show_alert=True)
                self.queue_reply(chat_id, "⚠️ 无法读取任务历史，请检查服务器状态；原记录已保留，没有重新执行任务。")
                return
            opened = False
            if record:
                opened = self.queue_reply(chat_id, describe_html(record), html_mode=True,
                                          reply_markup=self._result_buttons(record["job_id"]),
                                          result_key=(user, record["job_id"]))
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(
                    cq_id, text=("📖 正在打开任务结果" if opened else "⏳ 结果正在打开或刚刚已发送")
                    if record else "该历史记录不存在或已过期")
            return

        if data in {"tasks:history", "tasks:new"}:
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id)
            # Reuse the existing user-scoped command semantics, including deferred
            # /new during a running task. These buttons never launch a model job.
            if data == "tasks:new":
                self._offer_reset_confirmation(user)
            else:
                await self.handle({"message": {"chat": chat, "from": sender, "text": "/history"}})
            return

        if data in {"selector:models", "selector:close"}:
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id)
            if data == "selector:models":
                text, keyboard = self._model_picker(user)
            else:
                current = self.store.get_model(user) or self.settings.model or ""
                text, keyboard = f"已关闭选择\n当前：{self._model_label(current)}", {"inline_keyboard": []}
            if not await self._collapse_selector(chat_id, message, text, keyboard):
                self.queue_reply(chat_id, text, reply_markup=keyboard)
            return

        if data.startswith(("model:", "choose:")):
            choosing = data.startswith("choose:")
            target = data.split(":", 1)[1].strip()
            reset = target.lower() in {"default", "reset", "auto", "clear"} and not choosing
            resolved = resolve_model(target)
            if not reset and (is_retired_model(resolved) or not MODEL_RE.fullmatch(resolved) or
                              (choosing and resolved not in OFFICIAL_MODEL_IDS)):
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text="模型选项已失效，请重新打开 /model", show_alert=True)
                return
            # Family navigation does not persist a choice or depend on the high tier's health.
            if not reset and not choosing and selected_variant_effort(resolved):
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id)
                picker, keyboard = self._effort_picker(user, resolved, model=resolved)
                if not await self._collapse_selector(chat_id, message, picker, keyboard):
                    self.queue_reply(chat_id, picker, reply_markup=keyboard)
                return
            if not reset and self._model_is_known_unavailable(resolved):
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(
                        cq_id, text="该选项上次检测不可用，可用 /model refresh 重新检测", show_alert=True)
                return
            self.store.set_model(user, None if reset else resolved)
            self.store.set_effort(user, None)
            current = self.store.get_model(user) or self.settings.model or ""
            if cq_id and hasattr(self.api, "answer_callback_query"):
                self._answer_callback(cq_id, text="已恢复默认" if reset else "模型已切换")
            if not reset and supports_effort(resolved):
                picker, keyboard = self._effort_picker(user, resolved)
                if not await self._collapse_selector(chat_id, message, picker, keyboard):
                    self.queue_reply(chat_id, picker, reply_markup=keyboard)
                return
            text = f"{'已恢复默认' if reset else '已切换模型'}\n当前：{self._model_label(current)}\n后续任务生效"
            keyboard = {"inline_keyboard": [[
                {"text": "🤖 更换模型", "callback_data": "selector:models"},
                {"text": "✖️ 关闭", "callback_data": "selector:close"},
            ]]}
            if not await self._collapse_selector(chat_id, message, text, keyboard):
                self.queue_reply(chat_id, text, reply_markup=keyboard)
            return

        if data.startswith("effort:"):
            target = data[7:].strip().lower()
            model = self.store.get_model(user) or self.settings.model or ""
            if is_retired_model(model):
                self._answer_callback(cq_id, text="该模型已移除，请重新打开 /model", show_alert=True)
                return
            if target in {"low", "medium", "high", "max"}:
                variant = variant_model_for_effort(model, target, self._variant_ids())
                if variant is not None:
                    if self._model_is_known_unavailable(variant):
                        if cq_id and hasattr(self.api, "answer_callback_query"):
                            self._answer_callback(
                                cq_id, text="该思考档位当前检测为不可用", show_alert=True,
                            )
                        return
                    self.store.set_model(user, variant)
                    self.store.set_effort(user, None)
                    collapsed = f"✅ 已切换至：{variant}｜思考强度：{effort_label(None, variant)}"
                    if cq_id and hasattr(self.api, "answer_callback_query"):
                        self._answer_callback(cq_id, text=f"🎯 已切换至 {variant}")
                    if not await self._collapse_selector(chat_id, message, collapsed):
                        self.queue_reply(chat_id, collapsed)
                    return
                if selected_variant_effort(model):
                    if cq_id and hasattr(self.api, "answer_callback_query"):
                        self._answer_callback(cq_id, text="该模型暂不提供这一思考档位", show_alert=True)
                    return
            if not supports_effort(model):
                self.store.set_effort(user, None)
                collapsed = f"✅ 已切换至：{model}｜思考强度：模型内置"
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text="该模型使用内置思考强度")
                if not await self._collapse_selector(chat_id, message, collapsed):
                    self.queue_reply(chat_id, collapsed)
                return
            if target in {"default", "reset", "clear"}:
                self.store.set_effort(user, None)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text="🔄 已恢复默认思考强度")
                collapsed = f"✅ 已切换至：{model or '默认模型'}｜思考强度：默认"
                if not await self._collapse_selector(chat_id, message, collapsed):
                    self.queue_reply(chat_id, collapsed)
                return
            if target in {"low", "medium", "high"}:
                self.store.set_effort(user, target)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text=f"🎯 已设置思考强度为: {target.capitalize()}")
                collapsed = f"✅ 已切换至：{model or '默认模型'}｜思考强度：{effort_label(target, model)}"
                if not await self._collapse_selector(chat_id, message, collapsed):
                    self.queue_reply(chat_id, collapsed)
                return

        if data.startswith("mode:"):
            target = data[5:].strip().lower()
            if target in {"default", "reset", "clear"}:
                self.store.set_mode(user, None)
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text="🔄 已恢复默认执行模式")
                if not await self._collapse_selector(chat_id, message, "已恢复默认执行模式\n后续任务跟随 agy 设置"):
                    self.queue_reply(chat_id, "已恢复默认执行模式\n后续任务跟随 agy 设置")
                return
            if target in {"plan", "accept-edits"}:
                self.store.set_mode(user, target)
                name = "推演规划模式 (Plan)" if target == "plan" else "落地编辑模式 (Accept-Edits)"
                if cq_id and hasattr(self.api, "answer_callback_query"):
                    self._answer_callback(cq_id, text=f"🎯 已切换为: {name}")
                text = f"已切换模式：{name}\n后续任务生效"
                if not await self._collapse_selector(chat_id, message, text):
                    self.queue_reply(chat_id, text)
                return

    async def _flush_album(self) -> None:
        try:
            await asyncio.sleep(ALBUM_SETTLE_SECONDS)
            album = self.pending_album
            if album is None or self.stop.is_set():
                return
            self.pending_album = None
            first = dict(album["update"])
            message = dict(first["message"])
            message["caption"] = "\n".join(album["captions"])
            first["message"] = message
            await self.handle(first, album_items=album["items"])
        except asyncio.CancelledError:
            raise
        finally:
            if self.album_task is asyncio.current_task():
                self.album_task = None

    def _buffer_album(self, update: dict, user: int, group_id: str,
                      attachment: dict, is_photo: bool, caption: str) -> None:
        album = self.pending_album
        if album is not None and (album["user"], album["group_id"]) != (user, group_id):
            self.queue_reply(user, "⏳ 正在接收上一组附件，请稍后再发送新任务。")
            return
        if album is None:
            album = {"user": user, "group_id": group_id, "update": update,
                     "items": [], "captions": []}
            self.pending_album = album
        album["items"].append((attachment, is_photo))
        if caption.strip():
            album["captions"].append(caption.strip())
        if self.album_task is not None:
            self.album_task.cancel()
        self.album_task = asyncio.create_task(self._flush_album())

    async def handle(self, update: dict, *, album_items: list[tuple[dict, bool]] | None = None,
                     from_queue: bool = False) -> None:
        if self.stop.is_set():
            return
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self.handle_callback(callback_query)
            # Let the background acknowledgement start without waiting for its network request.
            await asyncio.sleep(0)
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
        caption = message.get("caption")
        if not isinstance(text, str):
            text = caption if isinstance(caption, str) else ""
        photo_list = message.get("photo")
        document = message.get("document")
        has_photo = isinstance(photo_list, list) and bool(photo_list)
        has_document = isinstance(document, dict)
        if (not text.strip()) and not has_photo and not has_document:
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
        force_queue = False
        if command == "/queue" and text.startswith("/queue add "):
            text = text[len("/queue add "):].strip()
            if not text or text.startswith("/"):
                self.queue_reply(chat_id, "💡 用法：/queue add <文字任务>。")
                return
            command = ""
            force_queue = True
        if not command and self._restart_pending():
            self.queue_reply(chat_id, "⏳ 正在重新载入服务，请稍后再发送任务或附件。")
            return
        if not command and any(not task.done() for task in self.steer_tasks):
            self.queue_reply(chat_id, "⏳ 正在交接修正任务，请等待清理和交接完成。")
            return
        group_id = message.get("media_group_id")
        if (album_items is None and isinstance(group_id, str) and group_id
                and (has_photo or has_document) and self.slot is None):
            item = (max((entry for entry in photo_list if isinstance(entry, dict)),
                        key=lambda entry: entry.get("file_size", 0)
                        if type(entry.get("file_size")) is int else 0, default=None)
                    if has_photo else document)
            if not isinstance(item, dict):
                self.queue_reply(chat_id, "⚠️ 无法识别这组附件，请重新发送。")
                return
            self._buffer_album(update, user, group_id, item, has_photo, text)
            return
        if command:
            await self.dispatch_command(command, text, user, chat_id, sys.modules[__name__])
            return
        attachment: dict | None = None
        is_photo = False
        if has_photo:
            candidates = [entry for entry in photo_list if isinstance(entry, dict)]
            if not candidates:
                self.queue_reply(chat_id, "⚠️ 无法识别这张图片，请重新发送原图或截图。")
                return
            attachment = max(candidates, key=lambda entry: entry.get("file_size", 0) if type(entry.get("file_size")) is int else 0)
            is_photo = True
        elif has_document:
            attachment = document

        items = album_items if album_items is not None else ([(attachment, is_photo)] if attachment else [])
        if (len(items) > MAX_ALBUM_FILES or
                sum(item.get("file_size", 0) if type(item.get("file_size")) is int else 0
                    for item, _ in items) > MAX_ALBUM_BYTES):
            self.queue_reply(chat_id, "⚠️ 一次最多发送 10 个附件，总大小不超过 30 MB。请分批发送。")
            return
        if len(text) > self.settings.max_prompt or "\x00" in text:
            self.queue_reply(chat_id, f"⚠️ 任务过长或含非法字符，最多 {self.settings.max_prompt} 个字符。")
            return
        if self.runner.blocked:
            self.queue_reply(chat_id, "⚠️ 新任务已暂停，请检查服务器并重启服务。")
            return
        if self.model_check_task and not self.model_check_task.done():
            self.queue_reply(chat_id, "⏳ 正在进行模型可用性检测，请完成后再提交任务。")
            return
        if self.slot is not None or self.pending_album is not None or (self.waiting_jobs and not from_queue):
            if (not items and not force_queue and self.slot is not None and self.slot.user == user
                    and self._offer_correction(user, text)):
                return
            if not force_queue and self.slot is not None and self.slot.user == user:
                self.queue_reply(chat_id, "⏳ 当前任务尚未结束。发送 /queue add <文字任务> 可排队，或等待清理完成。")
            elif items:
                self.queue_reply(chat_id, "⚠️ 附件暂不支持排队，请等待当前任务完成后重新发送。")
            elif len(self.waiting_jobs) >= 3:
                self.queue_reply(chat_id, "⚠️ 等待队列已满（最多 3 个）；本次没有接收，请稍后重发。")
            else:
                self.waiting_jobs.append((user, chat_id, text))
                self.queue_reply(chat_id, f"⏳ 已加入等待队列，当前位置 {len(self.waiting_jobs)}。前一个任务完成清理后开始；/queue cancel 可取消等待。服务重启后队列会丢失。")
                self._wake_queue()
            return

        user_model = self.store.get_model(user) or self.settings.model or ""
        if is_retired_model(user_model):
            self.queue_reply(chat_id, "当前保存的模型已移除，请先发送 /model 重新选择；本次没有启动任务。")
            return
        conv = self.store.get_conversation(user)
        conv_id = conv.get("conversation_id", "") if conv else ""
        effort = self.store.get_effort(user) or ""
        if not supports_effort(user_model):
            # Repair preferences saved by older versions before invoking agy.
            effort = ""
            self.store.set_effort(user, None)
        mode = self.store.get_mode(user) or ""
        fallback_title = f"分析附件：{items[0][0].get('file_name', '截图')}" if items else "未命名任务"
        job = Job(user, chat_id, model=user_model, conversation_id=conv_id, effort=effort,
                  mode=mode, history_title=task_title(text, fallback_title), original_prompt=text,
                  has_attachment=bool(items))
        self.slot = job  # reserve before first await
        try:
            self.store.maintain()
            self.store.save(user, {
                "job_id": job.job_id, "outcome": "running", "delivery": "pending",
                "title": job.history_title,
                "detail": "任务准备或执行中；服务中断时不会自动重试。",
                "model": job.model, "effort": job.effort, "mode": job.mode,
            })
            job.worker = asyncio.create_task(self._prepare_and_work(job, text, items))
        except Exception:
            self.slot = None
            LOG.error("job_prepare_failed id=%s", job.job_id)
            self.queue_reply(chat_id, "任务准备失败，未启动 agy。请检查服务器存储和权限。")

    async def _prepare_and_work(self, job: Job, text: str, items: list[tuple[dict, bool]]) -> None:
        """Download outside polling; retain the slot until all writes and cleanup finish."""
        job.phase = "receiving" if items else "preparing"
        chat_id = job.chat
        accepted = False
        try:
            prompt = text
            if items:
                try:
                    received = []
                    total_bytes = 0
                    for item, photo in items:
                        if job.cancel.is_set() or self.stop.is_set():
                            raise ValueError("attachment cancelled")
                        path, display_name = await self._receive_attachment(job, item, photo)
                        received.append((path, display_name))
                        total_bytes += path.stat().st_size
                        if total_bytes > MAX_ALBUM_BYTES:
                            raise ValueError("album too large")
                except (ValueError, OSError):
                    self._cleanup_attachments(job)
                    self.queue_reply(chat_id, "🛑 已取消附件接收，没有启动任务。" if job.cancel.is_set() else
                                     "⚠️ 附件未接收：仅支持截图或常见的文本、日志、代码文件，单文件最大 10MB；本组没有启动任务。")
                    return
                instruction = text or "请读取并分析该附件，说明发现的问题和建议。"
                paths = "\n".join(f"- {name}: {path}" for path, name in received)
                prompt = f"用户上传了 {len(received)} 个附件，路径如下：\n{paths}\n请先读取这些文件，再完成用户要求：{instruction}"
                if len(received) > 1:
                    names = "、".join(name for _, name in received)
                    job.attachment_feedback = f"📎 已收到 {len(received)} 个附件：{names}。本次任务会读取它们。"
            if len(prompt) > self.settings.max_prompt or "\x00" in prompt:
                self._cleanup_attachments(job)
                self.queue_reply(chat_id, f"⚠️ 任务过长或含非法字符，最多 {self.settings.max_prompt} 个字符。")
                return
            if job.cancel.is_set() or self.stop.is_set():
                self.queue_reply(chat_id, "🛑 已取消附件接收，没有启动任务。")
                return
            accepted = True
            await self._accept_and_work(job, prompt)
        except asyncio.CancelledError:
            job.cancel.set()
            raise
        except Exception as error:
            LOG.error("job_prepare_failed id=%s type=%s", job.job_id, type(error).__name__)
            self.queue_reply(chat_id, "任务准备失败，未启动 agy。请检查服务器存储和权限。")
        finally:
            self._cleanup_attachments(job)
            if not accepted:
                try:
                    record = self.store.save(job.user, {
                        "job_id": job.job_id, "outcome": "cancelled" if job.cancel.is_set() else "not_started",
                        "detail": "附件或任务准备未完成，没有启动 agy。",
                        "delivery": "unknown", "model": job.model, "effort": job.effort, "mode": job.mode,
                        "title": job.history_title,
                    })
                    self.store.save_history(job.user, job.history_title, record)
                    if job.reset_after:
                        self.store.reset_conversation(job.user)
                except (OSError, ValueError):
                    LOG.error("job_prepare_record_failed id=%s", job.job_id)
                    self.queue_reply(chat_id, "任务准备记录保存失败；请检查服务器。")
            self._release_job(job)

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
            message = update.get("message")
            stamp = message.get("date") if isinstance(message, dict) else None
            if type(stamp) is int:
                LOG.info("telegram_update_received age_seconds=%.2f", max(0.0, time.time() - stamp))
            await self.handle(update)

    async def initialize(self) -> None:
        me = await self.api.call("getMe")
        if not isinstance(me, dict) or me.get("is_bot") is not True:
            raise TelegramError()
        try:
            await self.api.set_commands([
                {"command": "start", "description": "欢迎页与快速开始"},
                {"command": "help", "description": "查看帮助与使用说明"},
                {"command": "status", "description": "查看当前任务状态"},
                {"command": "cancel", "description": "取消正在执行的任务"},
                {"command": "queue", "description": "查看或取消等待任务"},
                {"command": "steer", "description": "停止当前任务并按修正重做"},
                {"command": "model", "description": "选择模型与思考强度"},
                {"command": "new", "description": "新建对话并清除上下文"},
                {"command": "usage", "description": "查看 Token 用量"},
                {"command": "last", "description": "查看最近一次结果"},
                {"command": "history", "description": "查看最近 10 条任务"},
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
                    if hasattr(self.api, "get_updates"):
                        updates = await self.api.get_updates(
                            offset=self.offset, limit=25, timeout=25 if self.polling_ready else 0,
                            allowed_updates=["message", "callback_query"],
                        )
                    else:
                        # Small test doubles intentionally expose only call().
                        updates = await self.api.call(
                            "getUpdates", offset=self.offset, limit=25, timeout=25 if self.polling_ready else 0,
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
            self.waiting_jobs.clear()
            if self.queue_task is not None:
                self.queue_task.cancel()
                await asyncio.gather(self.queue_task, return_exceptions=True)
            if self.model_check_task is not None:
                check_task = self.model_check_task
                self._cancel_model_health()
                await asyncio.gather(check_task, return_exceptions=True)
                self._model_check_finished(check_task)
            if self.restart_task is not None:
                self.restart_task.cancel()
                await asyncio.gather(self.restart_task, return_exceptions=True)
                self.restart_task = None
            self.pending_album = None
            if self.album_task is not None:
                self.album_task.cancel()
                await asyncio.gather(self.album_task, return_exceptions=True)
            if self.ready_file is not None:
                self.ready_file.unlink(missing_ok=True)
            job = self.slot
            if job is not None:
                job.cancel.set()
            if self.reply_worker is not None:
                self.reply_worker.cancel()
                await asyncio.gather(self.reply_worker, return_exceptions=True)
            for task in self.callback_tasks:
                task.cancel()
            if self.callback_tasks:
                await asyncio.gather(*tuple(self.callback_tasks), return_exceptions=True)
            if job is not None and job.worker is not None:
                await job.worker
            if self.steer_tasks:
                await asyncio.gather(*tuple(self.steer_tasks), return_exceptions=True)
            for pending in self.reset_confirmations.values():
                if pending.timer is not None:
                    pending.timer.cancel()
            self.reset_confirmations.clear()


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
        bridge.api.close()
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

