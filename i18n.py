"""Localize project-owned interface elements without changing user content."""
from __future__ import annotations

import re
from contextvars import ContextVar

callback_language: ContextVar[str] = ContextVar("callback_language", default="zh")

_COMMANDS = (
    ("start", "欢迎页与快速开始", "Welcome and getting started"),
    ("language", "中文 / English", "Choose interface language"),
    ("help", "查看帮助与使用说明", "Help and usage"),
    ("status", "查看当前任务状态", "Current task status"),
    ("cancel", "取消正在执行的任务", "Cancel the current task"),
    ("queue", "查看或取消等待任务", "View or cancel waiting tasks"),
    ("steer", "停止当前任务并按修正重做", "Stop and rerun with a correction"),
    ("model", "选择模型与思考强度", "Choose model and reasoning effort"),
    ("new", "新建对话并清除上下文", "Start a new conversation"),
    ("usage", "查看 Token 用量", "Token usage"),
    ("last", "查看最近一次结果", "Latest result"),
    ("history", "查看最近 10 条任务", "Recent tasks"),
)


def bot_commands(language: str) -> list[dict[str, str]]:
    return [{"command": name, "description": en if language == "en" else zh}
            for name, zh, en in _COMMANDS]
_CALLBACK_NOTICES = {
    "任务已结束或这张进度卡已失效。": "This task or progress card has expired.",
    "正在展开过程，任务继续执行": "Expanding steps; the task continues",
    "正在收起过程并恢复自动刷新": "Collapsing steps and resuming automatic refresh",
    "正在刷新，保持当前展开状态": "Refreshing without changing the expanded state",
    "确认已失效，请重新点击“新建对话”。": "Confirmation expired; select New conversation again.",
    "已保留当前对话": "Current conversation retained",
    "对话已变化，请重新确认": "The conversation changed; confirm again",
    "已确认新建对话": "New conversation confirmed",
    "进度已刷新": "Progress refreshed",
    "检测已结束": "Check has ended",
    "已请求取消": "Cancellation requested",
    "检测已结束、按钮已失效或无取消权限": "Check ended, button expired, or cancellation is not permitted",
    "补充已失效，请确认当前任务后重新发送。": "The correction expired; review the current task and send it again.",
    "已提交修正": "Correction submitted",
    "已忽略补充": "Additional message ignored",
    "🛑 已请求取消": "🛑 Cancellation requested",
    "该任务已结束或不可取消": "This task has ended or cannot be cancelled",
    "这条等待任务已失效。": "This queued task is no longer available.",
    "已取消等待任务": "Queued task cancelled",
    "保存失败，请检查存储": "Could not save; check storage",
    "无法读取任务历史，请检查服务器状态。": "Could not read task history; check the server.",
    "📖 正在打开任务结果": "📖 Opening the task result",
    "⏳ 结果正在打开或刚刚已发送": "⏳ Result is opening or was just sent",
    "该历史记录不存在或已过期": "This history record is missing or expired",
    "模型选项已失效，请重新打开 /model": "This model option expired; reopen /model",
    "该选项上次检测不可用，可用 /model refresh 重新检测": "This option failed its last check; use /model refresh to check again",
    "该思考档位当前检测为不可用": "This reasoning level is marked unavailable",
    "该模型暂不提供这一思考档位": "This model does not offer that reasoning level",
    "该模型使用内置思考强度": "This model uses its built-in reasoning level",
    "模型已切换": "Model changed",
    "该模型已移除，请重新打开 /model": "This model was removed; reopen /model",
    "🔄 已恢复默认思考强度": "🔄 Default reasoning effort restored",
    "🔄 已恢复默认执行模式": "🔄 Default execution mode restored",
    "已切换模型": "Model changed",
    "已恢复默认": "Default restored",
    "已恢复默认执行模式": "Default execution mode restored",
    "🛑 已请求取消。正在清理任务进程；已经发生的修改不会自动撤销。":
        "🛑 Cancellation requested. Processes are being cleaned up; existing changes are not automatically undone.",
}


def english_callback_notice(value: str) -> str:
    if value in _CALLBACK_NOTICES:
        return _CALLBACK_NOTICES[value]
    for prefix, replacement in (("🎯 已切换至 ", "🎯 Switched to "),
                                ("🎯 已切换为: ", "🎯 Switched to: "),
                                ("🎯 已设置思考强度为: ", "🎯 Reasoning effort set to: ")):
        if value.startswith(prefix):
            return replacement + value[len(prefix):]
    return value

_BUTTONS = {
    "⬅️ 返回概览": "⬅️ Overview", "📋 详细信息": "📋 Details", "🔄 刷新状态": "🔄 Refresh",
    "🔄 刷新进度": "🔄 Refresh progress", "🛑 取消检测": "🛑 Cancel check",
    "⬅️ 返回模型": "⬅️ Back to models", "✖️ 关闭": "✖️ Close",
    "📥 收起过程": "📥 Collapse", "📋 展开过程": "📋 Expand",
    "🔄 刷新过程": "🔄 Refresh steps", "🛑 取消任务": "🛑 Cancel task",
    "📖 查看结果": "📖 View result", "📚 最近任务": "📚 Recent tasks",
    "🧠 新建对话": "🧠 New conversation", "🔎 检测当前模型": "🔎 Check current model",
    "🔎 检测全部": "🔎 Check all", "↩️ 恢复默认": "↩️ Reset to default",
    "🔄 立即刷新": "🔄 Refresh now",
    "🧠 确认新建": "🧠 Confirm new conversation", "↩️ 保留当前对话": "↩️ Keep conversation",
    "✏️ 作为修正": "✏️ Apply correction", "✖️ 忽略补充": "✖️ Ignore",
    "🤖 更换模型": "🤖 Change model", "编辑": "Edit", "规划": "Plan", "跟随默认": "Default",
}
_EFFORTS = {"极速": "Fast", "均衡": "Balanced", "深度": "Deep", "超高": "Extra high", "最高": "Maximum"}
_ACTIVITIES = {
    "模型会话已启动": "Model session started",
    "正在准备任务": "Preparing the task",
    "正在调用工具": "Calling a tool",
    "正在分析并生成回复": "Analyzing and generating a reply",
    "正在启动模型": "Starting the model",
    "正在继续处理任务": "Processing the task",
    "正在清理资源": "Cleaning up",
    "正在停止当前任务并应用你的修正": "Stopping the current task to apply your correction",
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
EN_OUTCOME_HELP = {
    "timed_out": "The task timed out and may have made partial changes. Check the workspace before deciding whether to retry.",
    "output_limit": "Output exceeded the limit. Narrow the task or inspect the workspace changes.",
    "invalid": "The result format was invalid; the task may have partially run. Check the workspace and /last.",
    "no_text": "No final text reply was received. Check the workspace and /last first.",
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
    "结果曾因存储异常暂存，恢复后已保存；本次会话和用量记录可能缺失。": "The result was temporarily held after a storage error and has now been saved; conversation and usage records may be missing.",
    "结果已保存，历史记录曾写入失败，恢复后已补存；会话和用量可能缺失。": "The result and recovered history are saved; conversation and usage records may be missing.",
    "任务准备或执行中；服务中断时不会自动重试。": "Task preparation or execution is in progress; it will not automatically retry if the service stops.",
    "附件或任务准备未完成，没有启动 agy。": "Attachment or task preparation did not finish; agy was not started.",
    "确认消息未成功投递、任务已取消或服务正在停止；没有启动 agy。": "The confirmation was not delivered, the task was cancelled, or the service is stopping; agy was not started.",
    "正在按用户修正重新执行；服务中断时不会自动重试。": "Running again with the user's correction; no automatic retry will occur if the service stops.",
    "任务结果已保存，但会话记录更新失败；请检查服务器。": "The result was saved but the conversation record could not be updated; check the server.",
    "任务结果已保存，但累计用量更新失败，本次统计可能缺失。": "The result was saved but cumulative usage could not be updated; this task's usage may be missing.",
    "服务在上次任务期间中断；任务可能产生了修改，没有自动重跑。": "The service stopped during the previous task. It may have made changes; it was not rerun automatically.",
}


def english_detail(value: str) -> str:
    if value in EN_DETAILS:
        return EN_DETAILS[value]
    match = re.fullmatch(r"agy 返回异常状态 \((.*?)\)：(.*)", value, flags=re.DOTALL)
    if match:
        return f"agy returned an error status ({match.group(1)}): {match.group(2)}"
    return value


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
