"""Telegram command handlers, separate from update and task scheduling."""
from __future__ import annotations

import asyncio
import html
import re
from collections import deque
from settings import MODEL_RE, is_retired_model, resolve_model


class CommandHandlers:
    async def _command_start(self, text, user, chat_id, helpers):
        self.queue_reply(
            chat_id,
            "👋 欢迎使用 Antigravity Telegram Remote\n━━━━━━━━━━━━━━━━━━━━\n"
            "这是你的专属 agy 远程工作台：直接发送一句需求，即可在服务器工作区执行。\n\n"
            "✅ 建议从这里开始：\n"
            "1. 发送 /model，选择模型与思考强度\n"
            "2. 直接发送任务，例如“检查当前项目的错误并修复”\n"
            "3. 运行中可点“取消任务”，或发送 /status 查看状态\n\n"
            "📎 可发送一组截图或文件（单个最大 10MB，整组最多 10 个、30MB）。\n"
            "🔒 仅白名单私聊可使用；任务会在你的服务器工作区运行。\n"
            "💡 发送 /help 查看全部指令，发送 /new 开启全新对话。",
        )
        return

    async def _command_help(self, text, user, chat_id, helpers):
        self.queue_control(
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
            "• ⏳ /queue - 查看等待任务；/queue add <内容> 排队，/queue cancel 取消等待\n"
            "• ⚡ /steer <修正内容> - 停止当前纯文字任务，按原任务加修正重新执行\n"
            "• 📜 /last - 查看最近一条任务的执行结果\n"
            "• 📚 /history - 查看最近 10 条任务并点开完整结果\n"
            "• 📎 可发送一组截图、日志或代码文件（单个最大 10MB，整组最多 10 个、30MB）\n"
            "• 🆔 /id - 查看你的 Telegram 数字 ID\n"
            "• ❓ /help - 显示帮助说明\n━━━━━━━━━━━━━━━━━━━━\n"
            "✨ 零依赖纯 Python 构建 ｜ 原生支持多轮上下文对话记忆！",
        )
        return

    async def _command_new(self, text, user, chat_id, helpers):
        self._close_reset_confirmation(user, "🧠 已通过命令新建对话。")
        self.queue_control(chat_id, self._reset_conversation(user))
        return

    async def _command_usage(self, text, user, chat_id, helpers):
        control_html = helpers.control_html
        usage = self.store.get_usage(user)
        conv = self.store.get_conversation(user)
        conv_info = "无活跃上下文（发送任意消息自动开启）"
        if conv and conv.get("conversation_id"):
            cid = conv["conversation_id"]
            short_cid = f"{cid[:8]}...{cid[-4:]}" if len(cid) > 16 else cid
            turns = conv.get("num_turns", 1)
            conv_info = f"`{short_cid}`（已连续对话 {turns} 轮）"
        report = control_html(f"📊 Token 用量统计报告\n💬 当前会话：{conv_info}")
        self.queue_reply(
            chat_id,
            report + "\n\n<blockquote>📈 历史累计资源消耗：\n"
            f"• 💬 累计对话轮次：{html.escape(str(usage.get('total_turns', 0)))} 轮\n"
            f"• 📥 输入 Token：{usage.get('total_input_tokens', 0):,}\n"
            f"• 📤 输出 Token：{usage.get('total_output_tokens', 0):,}\n"
            f"• 🧠 思考 Token：{usage.get('total_thinking_tokens', 0):,}\n"
            f"• 📊 总计 Token：{(usage.get('total_input_tokens', 0) + usage.get('total_output_tokens', 0)):,}\n"
            "</blockquote>\n💡 发送 /new 或 /reset 可重置当前会话上下文。",
            html_mode=True,
        )
        return

    async def _command_status(self, text, user, chat_id, helpers):
        details = text.split(maxsplit=1)[1:] == ["details"]
        summary, keyboard = self._status_card(user, details)
        self.queue_reply(chat_id, summary, reply_markup=keyboard, html_mode=details)
        return

    async def _command_queue(self, text, user, chat_id, helpers):
        if text.split(maxsplit=1)[1:] == ["cancel"]:
            before = len(self.waiting_jobs)
            self.waiting_jobs = deque(item for item in self.waiting_jobs if item[0] != user)
            self.queue_reply(chat_id, f"🛑 已移除你的 {before - len(self.waiting_jobs)} 个等待任务；正在执行的任务不受影响。")
        else:
            positions = [str(i) for i, item in enumerate(self.waiting_jobs, 1) if item[0] == user]
            self.queue_reply(chat_id, ("⏳ 你的任务排队位置：" + "、".join(positions) if positions
                                       else "ℹ️ 你没有等待中的任务。") + "\n/queue add <文字任务> 加入队列；/queue cancel 取消等待。服务重启后队列不会保留。")
        return

    async def _command_cancel(self, text, user, chat_id, helpers):
        job = self.slot
        if self.model_check_task is not None and not self.model_check_task.done():
            if user not in {self.model_check_user, self.settings.owner_id}:
                self.queue_reply(chat_id, "⚠️ 仅检测发起者或主管理员可以取消模型检测。")
                return
            self._cancel_model_health()
            self.queue_reply(chat_id, "🛑 已请求取消模型检测，正在等待进程清理完成；完成前不会接收新任务。")
        elif job is not None and job.user == user:
            job.cancel.set()
            job.steer_cancelled = True
            self.queue_reply(chat_id, "🛑 已请求取消。会清理任务进程；已经发生的修改不会自动撤销。")
        elif self.pending_album is not None and self.pending_album["user"] == user:
            self.pending_album = None
            if self.album_task is not None:
                self.album_task.cancel()
            self.queue_reply(chat_id, "🛑 已取消接收这组附件，没有启动任务。")
        else:
            self.queue_reply(chat_id, "ℹ️ 你当前没有可取消的任务。")
        return

    async def _command_last(self, text, user, chat_id, helpers):
        describe_html = helpers.describe_html
        try:
            record = self.store.load(user)
            if record:
                job_id = record.get("job_id", "")
                keyboard = self._result_buttons(job_id) if isinstance(job_id, str) and re.fullmatch(r"[a-f0-9]{12}", job_id) else None
                self.queue_reply(chat_id, describe_html(record), html_mode=True, reply_markup=keyboard,
                                 result_key=(user, record.get("job_id", "last")))
                return
            text = "ℹ️ 没有可取回的结果，或记录已过保留期。"
        except (OSError, ValueError):
            text = "⚠️ 无法读取最近结果，请检查服务器状态。"
        self.queue_reply(chat_id, text)
        return

    async def _command_steer(self, text, user, chat_id, helpers):
        correction_prompt = helpers.correction_prompt
        parts = text.split(maxsplit=1)
        correction = parts[1].strip() if len(parts) > 1 else ""
        if not correction or "\x00" in correction or len(correction) > self.settings.max_prompt:
            self.queue_reply(chat_id, "💡 用法：/steer <修正内容>。修正内容不能为空，且不能超过任务长度限制。")
            return
        job = self.slot
        if job is None:
            self.queue_reply(chat_id, "ℹ️ 当前没有运行中的任务，直接发送完整需求即可。")
            return
        if job.user != user:
            self.queue_reply(chat_id, "⚠️ 当前任务由其他白名单用户发起，不能由你修改。")
            return
        if job.has_attachment or job.attachments:
            self.queue_reply(chat_id, "📎 当前任务含附件。请先发送 /cancel，随后重新发送附件和完整的修正要求，避免误用已清理的临时文件。")
            return
        if job.steer_task is not None or job.cancel.is_set():
            self.queue_reply(chat_id, "⏳ 正在停止并清理当前任务；请等待新的任务卡出现后再继续修正。")
            return
        if not job.original_prompt:
            self.queue_reply(chat_id, "⚠️ 当前任务缺少可安全重用的原始内容。请取消后重新发送完整任务。")
            return
        if job.phase in {"saving", "sending"}:
            self.queue_reply(chat_id, "ℹ️ 当前任务已完成，正在保存或发送结果；请等待结束后发送新需求。")
            return
        if len(correction_prompt(job, correction)) > self.settings.max_prompt:
            self.queue_reply(chat_id, "⚠️ 原任务与修正合并后过长，当前任务未改变；请精简修正内容。")
            return
        self._forget_correction(job, "⚡ 已提交修正，正在等待清理和交接。")
        job.steered = True
        job.cancel.set()
        job.activity = "正在停止当前任务并应用你的修正"
        job.steer_task = asyncio.create_task(self._restart_with_correction(job, correction))
        self.steer_tasks.add(job.steer_task)
        job.steer_task.add_done_callback(self.steer_tasks.discard)
        job.steer_task.add_done_callback(lambda _: self._wake_queue())
        self.queue_reply(chat_id, "⚡ 已收到修正：正在安全停止当前任务，清理完成后会自动按新要求重新执行。")
        return

    async def _command_history(self, text, user, chat_id, helpers):
        history_summary = helpers.history_summary
        try:
            summary, keyboard = history_summary(self.store.history(user))
            self.queue_reply(chat_id, summary, reply_markup=keyboard)
        except (OSError, ValueError):
            self.queue_reply(chat_id, "⚠️ 无法读取任务历史，请检查服务器状态。")
        return

    async def _command_model(self, text, user, chat_id, helpers):
        has_effort_picker = helpers.has_effort_picker
        model_health_text = helpers.model_health_text
        parts = text.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if target.lower() in {"refresh", "check", "probe"}:
            self._start_model_health_refresh(chat_id)
            return
        if not target or target.lower() in {"show", "current", "status", "list", "help"}:
            picker, keyboard = self._model_picker(user)
            self.queue_reply(chat_id, picker, reply_markup=keyboard)
            return
        if target.lower() in {"default", "reset", "auto", "clear"}:
            self.store.set_model(user, None)
            self.store.set_effort(user, None)
            self.queue_reply(chat_id, "🔄 已恢复为默认模型（由 agy 决定）。\n💡 如需切换可随时使用 /model <模型名或别名>")
            return
        resolved = resolve_model(target)
        if is_retired_model(resolved):
            self.queue_reply(chat_id, "该模型已从项目移除，请发送 /model 选择其他模型。")
            return
        if not MODEL_RE.fullmatch(resolved):
            self.queue_reply(
                chat_id,
                "⚠️ 模型名称格式不正确。仅支持 2..64 个字母、数字、点、下划线与连字符。",
            )
            return
        if self._model_is_known_unavailable(resolved):
            state = self._model_health().get("models", {}).get(resolved)
            self.queue_reply(
                chat_id,
                f"⚠️ {resolved} 当前检测为不可用（{model_health_text(state)}）。\n"
                "发送 /model refresh 重新检测，或从 /model 的可用按钮中选择。",
            )
            return
        self.store.set_model(user, resolved)
        alias_note = f"（由别名 '{target}' 解析）" if resolved != target else ""
        if not has_effort_picker(resolved):
            self.store.set_effort(user, None)
            self.queue_reply(chat_id, f"✅ 已切换至：{resolved}｜思考强度：模型内置")
            return
        self._show_effort_picker(chat_id, user, f"{resolved}{alias_note}")
        return

    async def _command_sys(self, text, user, chat_id, helpers):
        system_status = helpers.system_status
        self.queue_reply(chat_id, system_status(self.settings.workspace, self._started_at))
        return

    async def _command_restart(self, text, user, chat_id, helpers):
        admin_id = self.settings.owner_id
        if user != admin_id:
            self.queue_reply(chat_id, f"⚠️ 仅主管理员（ID: {admin_id}）可以重启守护进程。")
            return
        if self._restart_busy():
            self.queue_reply(
                chat_id,
                "⚠️ 当前有正在执行的任务或资源尚未清理完成（含附件接收、模型检测）。请等待全部结束后再重启。"
            )
            return
        if self._restart_pending():
            self.queue_reply(chat_id, "⏳ 重启已安排，请稍候，不需要重复发送。")
            return
        self.queue_reply(chat_id, "🔄 服务将退出并由 systemd 重新启动，请稍后发送 /status 验证。"
                         + ("注意：等待队列不会保留，请重启后重新提交。" if self.waiting_jobs else ""))

        self.restart_task = asyncio.create_task(self._do_restart())
        return

    async def _command_whitelist(self, text, user, chat_id, helpers):
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
            self.queue_control(chat_id, "\n".join(lines))
            return

        if user != admin_id:
            self.queue_control(chat_id, f"⚠️ 仅主管理员（ID: {admin_id}）可管理动态白名单。")
            return

        if subcmd == "add":
            if not target_str.isdigit() or not (5 <= len(target_str) <= 16):
                self.queue_control(chat_id, "⚠️ 请输入合法的 Telegram 数字用户 ID（5-16 位正整数）。\n例如：/whitelist add 123456789")
                return
            target_id = int(target_str)
            if target_id in self.settings.allowed:
                self.queue_control(chat_id, f"ℹ️ 用户 `{target_id}` 已在 config.env 基础配置中，无需重复添加。")
                return
            ok = self.store.add_whitelist(target_id)
            if ok:
                self.queue_control(chat_id, f"✅ 已成功添加用户 `{target_id}` 到授权白名单！该用户现在可以私聊使用此 Bot。")
            else:
                self.queue_control(chat_id, f"ℹ️ 用户 `{target_id}` 已经在动态白名单中了。")
            return

        if subcmd in {"remove", "del", "delete", "rm"}:
            if not target_str.isdigit():
                self.queue_control(chat_id, "⚠️ 请指定要移除的 Telegram 数字用户 ID。\n例如：/whitelist remove 123456789")
                return
            target_id = int(target_str)
            if target_id in self.settings.allowed:
                self.queue_control(chat_id, f"⚠️ 用户 `{target_id}` 属于 config.env 基础配置白名单，无法通过指令移除。")
                return
            ok = self.store.remove_whitelist(target_id)
            if ok:
                self.queue_control(chat_id, f"🗑️ 已成功从动态白名单中移除用户 `{target_id}`。")
            else:
                self.queue_control(chat_id, f"⚠️ 未在动态白名单中找到用户 `{target_id}`。")
            return

        self.queue_control(chat_id, "⚠️ 未知白名单指令。用法：\n• 查看：/whitelist\n• 添加：/whitelist add <ID>\n• 移除：/whitelist remove <ID>")
        return

    async def _command_mode(self, text, user, chat_id, helpers):
        parts = text.split(maxsplit=1)
        target = parts[1].strip().lower() if len(parts) > 1 else ""
        if not target or target in {"show", "current", "list", "help"}:
            curr = self.store.get_mode(user)
            curr_display = "推演规划模式 (Plan)" if curr == "plan" else ("落地编辑模式 (Accept-Edits)" if curr == "accept-edits" else "跟随 agy 设置（不保证自动编辑）")
            lines = [
                f"执行模式\n当前：{curr_display}",
                "编辑：自动接受文件编辑，命令仍受工具审批与系统权限限制。",
                "规划：先给出方案，不是系统只读隔离。",
            ]
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": f"{'✓ ' if curr == 'accept-edits' else ''}编辑", "callback_data": "mode:accept-edits"},
                        {"text": f"{'✓ ' if curr == 'plan' else ''}规划", "callback_data": "mode:plan"},
                    ],
                    [
                        {"text": f"{'✓ ' if not curr else ''}跟随默认", "callback_data": "mode:default"},
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
            self.queue_reply(chat_id, "📋 已切换为【推演规划模式 (plan)】！\n💡 先分析并给出计划；这是规划指令，不是操作系统只读沙箱。")
            return
        if target in {"code", "edit", "edits", "accept-edits", "落地", "编辑"}:
            self.store.set_mode(user, "accept-edits")
            self.queue_reply(chat_id, "🛠️ 已切换为【落地编辑模式 (accept-edits)】！\n💡 在此模式下，AI 将直接在工作目录中修改并应用代码。")
            return
        self.queue_reply(chat_id, "⚠️ 不支持的执行模式。可选：`/mode plan` (推演规划), `/mode code` (落地编辑), `/mode default` (默认)")
        return

    async def _command_ls(self, text, user, chat_id, helpers):
        format_file_entry = helpers.format_file_entry
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
                self.queue_control(chat_id, "⚠️ 路径不合法：严禁越权访问工作空间以外的目录。")
                return
            if not candidate.exists() or not candidate.is_dir():
                self.queue_control(chat_id, f"⚠️ 子目录 `{subpath}` 不存在或并非目录。")
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
            self.queue_control(chat_id, "\n".join(lines))
        except Exception:
            self.queue_control(chat_id, "⚠️ 无法读取工作空间目录，请检查服务器权限。")
        return

    async def dispatch_command(self, command, text, user, chat_id, helpers):
        handlers = {
            '/start': self._command_start,
            '/help': self._command_help,
            '/new': self._command_new,
            '/reset': self._command_new,
            '/usage': self._command_usage,
            '/status': self._command_status,
            '/queue': self._command_queue,
            '/cancel': self._command_cancel,
            '/last': self._command_last,
            '/steer': self._command_steer,
            '/history': self._command_history,
            '/model': self._command_model,
            '/sys': self._command_sys,
            '/system': self._command_sys,
            '/restart': self._command_restart,
            '/whitelist': self._command_whitelist,
            '/mode': self._command_mode,
            '/ls': self._command_ls,
            '/files': self._command_ls,
        }
        handler = handlers.get(command)
        if handler is None:
            self.queue_reply(chat_id, "⚠️ 不支持这个控制命令。发送 /help 查看可用指令。")
            return
        await handler(text, user, chat_id, helpers)
