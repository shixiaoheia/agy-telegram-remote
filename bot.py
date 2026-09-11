import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AGY = os.getenv("AGY_PATH", "agy")
WORK = Path(os.getenv("AGY_WORKSPACE", "/srv/agy-workspace")).resolve()
TIMEOUT = int(os.getenv("AGY_TIMEOUT_SECONDS", "900"))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "12000"))
ALLOWED = {int(value) for value in os.getenv("ALLOWED_USER_IDS", "").split(",") if value.strip()}
RUNNING: dict[int, asyncio.subprocess.Process] = {}
CANCELED: set[int] = set()


def authorized(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ALLOWED)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text(f"你的 Telegram 数字 ID：{update.effective_user.id}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if authorized(update):
        await update.message.reply_text("直接发送任务给 agy。\n/status 查看状态\n/cancel 停止当前任务")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if authorized(update):
        text = "任务运行中。" if update.effective_user.id in RUNNING else "当前没有任务。"
        await update.message.reply_text(text)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    user_id = update.effective_user.id
    process = RUNNING.get(user_id)
    if not process or process.returncode is not None:
        await update.message.reply_text("当前没有可停止的任务。")
        return
    CANCELED.add(user_id)
    process.terminate()
    await update.message.reply_text("已请求停止任务。")


def parse_agy_output(raw: bytes) -> tuple[str, str]:
    streamed: list[str] = []
    answer = ""
    error = ""
    for line in raw.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "step_update":
            delta = (event.get("step_update") or {}).get("text_delta")
            if delta:
                streamed.append(delta)
        if event.get("event") == "result":
            result = event.get("result") or {}
            answer = result.get("response") or answer
            if result.get("status") != "SUCCESS":
                error = result.get("error") or f"agy 返回状态：{result.get('status', '未知')}"
    return answer or "".join(streamed), error


async def send_long(update: Update, text: str) -> None:
    for start in range(0, len(text), 3900):
        await update.message.reply_text(text[start : start + 3900])


async def task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update) or not update.effective_chat or update.effective_chat.type != "private":
        return
    prompt = (update.message.text or "").strip()
    if not prompt:
        return
    if len(prompt) > MAX_PROMPT_CHARS:
        await update.message.reply_text(f"任务过长，请控制在 {MAX_PROMPT_CHARS} 个字符以内。")
        return
    if not WORK.is_dir():
        await update.message.reply_text(f"工作目录不存在或不可用：{WORK}")
        return

    user_id = update.effective_user.id
    note = await update.message.reply_text("任务已接收，正在调用 agy…")
    process = None
    try:
        # Use only documented headless-mode flags. The work directory is supplied via cwd.
        command = [
            AGY,
            "--print",
            prompt,
            "--print-timeout",
            f"{TIMEOUT}s",
            "--output-format",
            "stream-json",
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=WORK,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        RUNNING[user_id] = process
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT + 30)

        if user_id in CANCELED:
            await note.edit_text("任务已取消。")
            return

        answer, error = parse_agy_output(stdout)
        if process.returncode != 0 and not error:
            error = stderr.decode(errors="replace").strip() or f"agy 退出码：{process.returncode}"
        if error:
            await note.edit_text(f"执行失败：{error[:3500]}")
            return
        if not answer:
            await note.edit_text("agy 没有返回内容。")
            return

        await note.edit_text("任务完成：")
        await send_long(update, answer)
    except asyncio.TimeoutError:
        if process and process.returncode is None:
            process.kill()
            await process.communicate()
        await note.edit_text("任务超时，已停止。")
    except FileNotFoundError:
        await note.edit_text(f"未找到 agy：{AGY}")
    except Exception as exc:
        await note.edit_text(f"执行失败：{str(exc)[:3500]}")
    finally:
        RUNNING.pop(user_id, None)
        CANCELED.discard(user_id)


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler(["start", "help"], help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, task))
    app.run_polling()


if __name__ == "__main__":
    main()
