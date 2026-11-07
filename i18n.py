"""Localize project-owned interface elements without changing user content."""
from __future__ import annotations

import re

_BUTTONS = {
    "⬅️ 返回概览": "⬅️ Overview", "📋 详细信息": "📋 Details", "🔄 刷新状态": "🔄 Refresh",
    "🔄 刷新进度": "🔄 Refresh progress", "🛑 取消检测": "🛑 Cancel check",
    "⬅️ 返回模型": "⬅️ Back to models", "✖️ 关闭": "✖️ Close",
    "📥 收起过程": "📥 Collapse", "📋 展开过程": "📋 Expand",
    "🔄 刷新过程": "🔄 Refresh steps", "🛑 取消任务": "🛑 Cancel task",
    "📖 查看结果": "📖 View result", "📚 最近任务": "📚 Recent tasks",
    "🧠 新建对话": "🧠 New conversation", "🔎 检测当前模型": "🔎 Check current model",
    "🔎 检测全部": "🔎 Check all", "↩️ 恢复默认": "↩️ Reset to default",
    "🧠 确认新建": "🧠 Confirm new conversation", "↩️ 保留当前对话": "↩️ Keep conversation",
    "✏️ 作为修正": "✏️ Apply correction", "✖️ 忽略补充": "✖️ Ignore",
    "🤖 更换模型": "🤖 Change model", "编辑": "Edit", "规划": "Plan", "跟随默认": "Default",
}
_EFFORTS = {"极速": "Fast", "均衡": "Balanced", "深度": "Deep", "最高": "Maximum"}
_ACTIVITIES = {
    "模型会话已启动": "Model session started",
    "正在调用工具": "Calling a tool",
    "正在分析并生成回复": "Analyzing and generating a reply",
    "正在启动模型": "Starting the model",
    "正在继续处理任务": "Processing the task",
    "正在清理资源": "Cleaning up",
}
EN_OUTCOMES = {
    "success": "✅ Task completed", "no_text": "⚠️ No final reply",
    "permission": "🚫 Permission denied", "invalid": "⚠️ Invalid result",
    "error": "❌ Task failed", "timed_out": "⏱️ Task timed out",
    "cancelled": "🛑 Task cancelled", "interrupted": "⚠️ Task interrupted",
    "output_limit": "📏 Output limit reached", "cleanup_failed": "⚠️ Cleanup not confirmed",
    "not_started": "⏹️ Task did not start", "running": "⏳ Task running",
}
EN_CATEGORY_HELP = {
    "location": "Google rejected API access from this location. Check the server exit and account region; refreshing models or signing in again may not help.",
    "auth": "Authorization is needed. Run agy auth login on the server before retrying.",
    "quota": "Account quota or rate limit reached. Check your quota or try later.",
    "capacity": "The model has no capacity. Check any changes already made before deciding whether to resubmit; the task was not rerun automatically.",
    "model": "The model or its parameters are unavailable. Use /model refresh to check.",
    "entitlement": "The account or plan is not entitled to this model. Select an available model; use /model refresh after changing accounts or plans.",
    "permission": "Tool approval was denied. Check auto-approval and agy permissions in /status; use /mode code when editing is needed.",
    "filesystem_readonly": "The filesystem is read-only. Auto-approval cannot remove systemd restrictions; explicitly allow required paths with --write-paths.",
    "filesystem_permission": "System permission denied. Check file permissions, ACLs and service capabilities; auto-approval cannot grant system permissions.",
    "network": "Server network error. Check DNS, proxy, and Google connectivity.",
    "process": "Could not start agy. Check its installation and permissions.",
}
EN_DETAILS = {
    "agy 未返回完整、有效的 JSON 结果；任务可能已产生修改。": "agy did not return a complete valid JSON result; the task may already have made changes.",
    "agy 结果不是 JSON 对象。": "The agy result is not a JSON object.",
    "agy 结果缺少有效的最终状态。": "The agy result has no valid final status.",
    "agy 返回异常状态；已执行的操作不会自动撤销。": "agy returned an error status; completed operations are not automatically undone.",
    "agy 的 response 缺失或不是文本。": "The agy response is missing or is not text.",
    "agy 返回了无效的 Unicode 文本。": "agy returned invalid Unicode text.",
    "检测到权限或文件系统限制；请核对任务实际完成情况。": "A permission or filesystem restriction was detected; check what the task actually completed.",
    "agy 已结束，但缺少最终文字回复；请先核对执行记录和工作目录。": "agy finished without a final text reply; check the execution record and workspace.",
    "agy 未返回完整、有效的事件流结果；任务可能已产生修改。": "agy did not return a complete valid event stream; the task may already have made changes.",
    "上次进程清理未确认完成；请重启服务后检查。": "Previous process cleanup was not confirmed; restart the service and inspect it.",
    "任务在启动前已取消。": "The task was cancelled before launch.",
    "任务为空、过长或含非法字符。": "The task is empty, too long, or contains invalid characters.",
    "agy 启动或执行异常。": "agy failed to start or run.",
    "已请求取消；此前完成的操作不会自动撤销。": "Cancellation requested; completed operations are not automatically undone.",
    "输出超过保留上限，任务已停止；不解析截断数据。": "Output exceeded the limit and the task stopped; truncated data was not parsed.",
    "任务超时，已请求停止；请核对已有修改。": "The task timed out and was asked to stop; check existing changes.",
    "服务停止或任务被取消。": "The service stopped or the task was cancelled.",
    "无法启动或管理 agy；请检查程序、目录与权限。": "Could not start or manage agy; check the program, directories, and permissions.",
    "未能确认全部子进程与管道已清理；已暂停接收新任务。": "Cleanup of all subprocesses and pipes could not be confirmed; new tasks are paused.",
    "任务输入或输出读取未确认完成；请核对已有修改。": "Task input or output could not be fully confirmed; check existing changes.",
    "输出超过上限；没有解析或转发截断数据。": "Output exceeded the limit; truncated data was not parsed or forwarded.",
    "任务已取消，请核对已有修改。": "The task was cancelled; check any changes already made.",
}


def english_activity(value: str) -> str:
    """Translate known generated progress labels, keeping tool names and commands intact."""
    if value in _ACTIVITIES:
        return _ACTIVITIES[value]
    prefixes = {
        "正在调用工具：": "Calling tool: ",
        "接收附件 ": "Receiving attachments ",
        "✅ ✍️ 回答生成完毕": "✅ ✍️ Reply generated",
        "✅ 💻 执行命令 ": "✅ 💻 Executed command ",
        "✅ 🛠️ 工具步骤完成": "✅ 🛠️ Tool step completed",
        "✅ 🧠 规划步骤完成": "✅ 🧠 Planning step completed",
    }
    for prefix, replacement in prefixes.items():
        if value.startswith(prefix):
            result = replacement + value[len(prefix):]
            return re.sub(r"（耗时 ([0-9.]+) 秒）$", r" (\1 s)", result)
    return value


def english_attachment(value: str) -> str:
    """Translate fixed attachment labels without changing filenames."""
    single = re.fullmatch(r"📎 已收到：[^「]*「(.*)」，(.*?)。本次任务会读取它。", value)
    if single:
        return f"📎 Received: {single.group(1)} ({single.group(2)}). The task will read it."
    group = re.fullmatch(r"📎 已收到 ([0-9]+) 个附件：(.*)。本次任务会读取它们。", value)
    if group:
        return f"📎 Received {group.group(1)} attachments: {group.group(2)}. The task will read them."
    return value


def localize_markup(markup: dict | None, language: str) -> dict | None:
    """Copy known static buttons only; never translate history titles or task text."""
    if language != "en" or not isinstance(markup, dict):
        return markup
    rows = markup.get("inline_keyboard")
    if not isinstance(rows, list):
        return markup
    result = dict(markup)
    copied = []
    for row in rows:
        if not isinstance(row, list):
            copied.append(row)
            continue
        items = []
        for button in row:
            if not isinstance(button, dict):
                items.append(button)
                continue
            item = dict(button)
            text, callback = item.get("text"), item.get("callback_data")
            if isinstance(text, str) and isinstance(callback, str):
                prefix = "✓ " if text.startswith("✓ ") else ""
                body = text[len(prefix):]
                if body in _BUTTONS:
                    item["text"] = prefix + _BUTTONS[body]
                elif callback.startswith("queue:cancel:"):
                    match = re.fullmatch(r"🛑 取消第 ([0-9]+) 项", body)
                    if match:
                        item["text"] = prefix + f"🛑 Cancel item {match.group(1)}"
                elif callback.startswith(("choose:", "effort:")):
                    for zh, en in _EFFORTS.items():
                        if body.endswith(" " + zh):
                            item["text"] = prefix + body[:-len(zh)] + en
                            break
            items.append(item)
        copied.append(items)
    result["inline_keyboard"] = copied
    return result
