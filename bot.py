import asyncio
import json
import os
from collections import deque
import signal
from dataclasses import dataclass
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
MAX_OUTPUT_BYTES = max(1024, int(os.getenv("MAX_OUTPUT_BYTES", "1048576")))
MAX_REPLY_CHARS = max(3900, int(os.getenv("MAX_REPLY_CHARS", "30000")))
SKIP_PERMISSIONS = os.getenv("AGY_SKIP_PERMISSIONS", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ALLOWED = {
    int(value)
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}


@dataclass
class Job:
    process: asyncio.subprocess.Process | None = None
    cancelled: bool = False
    state: str = "准备中"
    worker: asyncio.Task | None = None
    communicate_task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None


# A single dedicated workspace is shared by all allowed users, so it has one slot.
JOBS: dict[int, Job] = {}
WORK_LOCK = asyncio.Lock()
WORK_SLOT: Job | None = None


def is_private_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


def authorized(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ALLOWED)


def authorized_private(update: Update) -> bool:
    return is_private_chat(update) and authorized(update)


def signal_process_group(process: asyncio.subprocess.Process, signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def decode_output(raw: bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode(errors="replace").strip()
    return str(raw).strip()


def parse_agy_output(raw: bytes | None) -> tuple[str, str]:
    if not raw:
        return "", ""

    streamed: list[str] = []
    answer = ""
    error = ""

    for line in decode_output(raw).splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        if event.get("event") == "step_update":
            step_update = event.get("step_update")
            if isinstance(step_update, dict):
                delta = step_update.get("text_delta")
                if isinstance(delta, str):
                    streamed.append(delta)

        if event.get("event") == "result":
            result = event.get("result")
            if not isinstance(result, dict):
                error = "agy 返回格式异常：result 不是对象。"
                continue

            response = result.get("response")
            if isinstance(response, str):
                answer = response
            elif response is not None:
                error = "agy 返回格式异常：response 不是文本。"

            status = result.get("status")
            if status != "SUCCESS":
                result_error = result.get("error")
                error = (
                    str(result_error)
                    if result_error not in (None, "")
                    else f"agy 返回状态：{status or '未知'}"
                )

    return answer or "".join(streamed), error


async def read_output_tail(
    stream: asyncio.StreamReader | None, limit: int
) -> tuple[bytes, bool]:
    """Drain a pipe completely while retaining only its most recent bytes."""
    if stream is None:
        return b"", False

    chunks: deque[bytes] = deque()
    stored = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        stored += len(chunk)
        while stored > limit:
            first = chunks[0]
            excess = stored - limit
            if len(first) <= excess:
                chunks.popleft()
                stored -= len(first)
            else:
                chunks[0] = first[excess:]
                stored -= excess
            truncated = True
    return b"".join(chunks), truncated


async def collect_process_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes, bool, bool]:
    stdout_task = asyncio.create_task(read_output_tail(process.stdout, MAX_OUTPUT_BYTES))
    stderr_task = asyncio.create_task(read_output_tail(process.stderr, MAX_OUTPUT_BYTES))
    try:
        await process.wait()
        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task
        return stdout, stderr, stdout_truncated, stderr_truncated
    finally:
        for reader_task in (stdout_task, stderr_task):
            if not reader_task.done():
                reader_task.cancel()


async def send_long(update: Update, text: str) -> None:
    if not update.message:
        return
    if len(text) > MAX_REPLY_CHARS:
        notice = "\n\n（回传内容过长，已截断；请在服务器工作目录或日志中查看完整输出。）"
        text = text[: max(0, MAX_REPLY_CHARS - len(notice))] + notice
    for start in range(0, len(text), 3900):
        await update.message.reply_text(text[start : start + 3900])


async def terminate_and_reap(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task | None,
) -> None:
    """Stop the agy process group, including child tools, and reap its pipes."""
    if process.returncode is None:
        signal_process_group(process, signal.SIGTERM)

    try:
        if communicate_task is not None:
            await asyncio.wait_for(asyncio.shield(communicate_task), timeout=10)
        else:
            await asyncio.wait_for(process.wait(), timeout=10)
        return
    except asyncio.CancelledError:
        # Shutdown may cancel the reaper. Escalate before preserving cancellation.
        if process.returncode is None:
            signal_process_group(process, signal.SIGKILL)
        raise
    except Exception:
        pass

    if process.returncode is None:
        signal_process_group(process, signal.SIGKILL)

    try:
        if communicate_task is not None:
            await asyncio.wait_for(asyncio.shield(communicate_task), timeout=5)
        else:
            await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        # A detached descendant may keep a pipe open after its parent exits.
        # Do not leave this bot worker blocked forever on that pipe.
        if communicate_task is not None and not communicate_task.done():
            communicate_task.cancel()
    except Exception:
        pass


def ensure_reaper(
    application: Application,
    update: Update,
    job: Job,
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task | None,
) -> asyncio.Task:
    """Create at most one live reaper for this Job without yielding first."""
    current = job.stop_task
    if current is None or (current.done() and process.returncode is None):
        cleanup = terminate_and_reap(process, communicate_task)
        try:
            current = application.create_task(cleanup, update=update)
        except BaseException:
            cleanup.close()
            if process.returncode is None:
                signal_process_group(process, signal.SIGKILL)
            raise
        job.stop_task = current
    return current


def start_reaper(application: Application, update: Update, job: Job) -> None:
    process = job.process
    if process is not None and process.returncode is None:
        ensure_reaper(application, update, job, process, job.communicate_task)


async def finish_reaper(
    application: Application,
    update: Update,
    job: Job,
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task | None,
) -> None:
    stop_task = ensure_reaper(application, update, job, process, communicate_task)
    try:
        await asyncio.shield(stop_task)
    except asyncio.CancelledError:
        if process.returncode is None:
            signal_process_group(process, signal.SIGKILL)
        raise
    except Exception:
        pass


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update) and update.effective_user and update.message:
        await update.message.reply_text(
            f"你的 Telegram 数字 ID：{update.effective_user.id}"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if authorized_private(update) and update.message:
        await update.message.reply_text(
            "直接发送任务给 agy。\n"
            "/status 查看状态\n"
            "/cancel 停止当前任务"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized_private(update) or not update.message or not update.effective_user:
        return

    job = JOBS.get(update.effective_user.id)
    if job is None:
        await update.message.reply_text("当前没有任务。")
    else:
        await update.message.reply_text(f"当前任务：{job.state}。")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized_private(update) or not update.message or not update.effective_user:
        return

    job = JOBS.get(update.effective_user.id)
    if job is None:
        await update.message.reply_text("当前没有可停止的任务。")
        return

    job.cancelled = True
    if job.process is None:
        await update.message.reply_text("任务正在准备，已请求取消。")
    elif job.process.returncode is None:
        signal_process_group(job.process, signal.SIGTERM)
        start_reaper(context.application, update, job)
        await update.message.reply_text("已请求停止任务及其子进程。")
    else:
        await update.message.reply_text("agy 已结束，结果正在发送。")


async def run_job(
    application: Application,
    update: Update,
    note,
    prompt: str,
    user_id: int,
    job: Job,
) -> None:
    global WORK_SLOT

    process = None
    communicate_task = None
    successful = False

    try:
        async with WORK_LOCK:
            if job.cancelled:
                job.state = "已取消"
                await note.edit_text("任务已取消。")
                return

            command = [
                AGY,
                "--print",
                prompt,
                "--print-timeout",
                f"{TIMEOUT}s",
                "--output-format",
                "stream-json",
            ]
            if SKIP_PERMISSIONS:
                command.append("--dangerously-skip-permissions")

            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=WORK,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            job.process = process
            job.state = "运行中"
            communicate_task = asyncio.create_task(collect_process_output(process))
            job.communicate_task = communicate_task

            if job.cancelled:
                job.state = "已取消"
                await finish_reaper(
                    application, update, job, process, communicate_task
                )
                await note.edit_text("任务已取消，子进程已回收。")
                return

            try:
                (
                    stdout,
                    stderr,
                    stdout_truncated,
                    stderr_truncated,
                ) = await asyncio.wait_for(
                    asyncio.shield(communicate_task),
                    timeout=TIMEOUT + 30,
                )
            except asyncio.TimeoutError:
                if job.cancelled:
                    job.state = "已取消"
                    await finish_reaper(
                        application, update, job, process, communicate_task
                    )
                    await note.edit_text("任务已取消，子进程已回收。")
                else:
                    job.state = "超时"
                    await finish_reaper(
                        application, update, job, process, communicate_task
                    )
                    await note.edit_text("任务超时，已停止并回收子进程。")
                return

            if job.cancelled:
                job.state = "已取消"
                await finish_reaper(
                    application, update, job, process, communicate_task
                )
                await note.edit_text("任务已取消，子进程已回收。")
                return

            answer, error = parse_agy_output(stdout)
            stderr_text = decode_output(stderr)
            if stdout_truncated:
                output_note = (
                    "agy 输出超过保留上限，已只保留最后 "
                    f"{MAX_OUTPUT_BYTES // 1024} KiB。"
                )
                if answer:
                    answer = f"{answer}\n\n（{output_note}）"
                elif not error:
                    error = output_note
            if stderr_truncated and not error and process.returncode != 0:
                error = "agy 错误输出过长，已只保留最后部分诊断信息。"
            if process.returncode != 0 and not error:
                error = stderr_text or f"agy 退出码：{process.returncode}"
            elif "soft-denied" in stderr_text.lower():
                error = (
                    "agy 有工具请求被权限策略拒绝。"
                    "如确认白名单、工作目录和任务都可信，可在 .env 中把 "
                    "AGY_SKIP_PERMISSIONS 改为 true 后重启服务。\n\n"
                    f"{stderr_text[-1800:]}"
                )

            if error:
                job.state = "失败"
                await finish_reaper(
                    application, update, job, process, communicate_task
                )
                await note.edit_text(f"执行失败：{str(error)[:3500]}")
                return
            if not answer:
                job.state = "失败"
                await finish_reaper(
                    application, update, job, process, communicate_task
                )
                await note.edit_text("agy 没有返回内容。")
                return

            successful = True
            job.state = "完成"
            await note.edit_text("任务完成：")
            await send_long(update, answer)
    except asyncio.CancelledError:
        job.cancelled = True
        job.state = "已取消"
        if process is not None:
            await finish_reaper(
                application, update, job, process, communicate_task
            )
        raise
    except FileNotFoundError:
        job.state = "失败"
        await note.edit_text(f"未找到 agy：{AGY}")
    except Exception as exc:
        job.state = "失败"
        await note.edit_text(f"执行失败：{str(exc)[:3500]}")
    finally:
        if process is not None and not successful:
            await finish_reaper(
                application, update, job, process, communicate_task
            )

        job.process = None
        job.communicate_task = None
        if JOBS.get(user_id) is job:
            JOBS.pop(user_id, None)
        if WORK_SLOT is job:
            WORK_SLOT = None


async def task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global WORK_SLOT

    if not authorized_private(update) or not update.message or not update.effective_user:
        return

    prompt = (update.message.text or "").strip()
    if not prompt:
        return
    if len(prompt) > MAX_PROMPT_CHARS:
        await update.message.reply_text(
            f"任务过长，请控制在 {MAX_PROMPT_CHARS} 个字符以内。"
        )
        return
    if not WORK.is_dir():
        await update.message.reply_text(f"工作目录不存在或不可用：{WORK}")
        return

    user_id = update.effective_user.id
    if user_id in JOBS:
        await update.message.reply_text("你已有任务在运行，请先等待完成或发送 /cancel。")
        return
    if WORK_SLOT is not None:
        await update.message.reply_text(
            "工作目录正在执行另一个任务。为避免同时读写冲突，请等待后重试。"
        )
        return

    # Register before the first await. The short handler returns immediately;
    # the long agy run is managed by the Application as a background task.
    job = Job()
    JOBS[user_id] = job
    WORK_SLOT = job

    try:
        note = await update.message.reply_text("任务已接收，正在调用 agy…")
    except BaseException:
        if JOBS.get(user_id) is job:
            JOBS.pop(user_id, None)
        if WORK_SLOT is job:
            WORK_SLOT = None
        raise

    worker_coro = run_job(context.application, update, note, prompt, user_id, job)
    try:
        job.worker = context.application.create_task(worker_coro, update=update)
    except BaseException:
        worker_coro.close()
        if JOBS.get(user_id) is job:
            JOBS.pop(user_id, None)
        if WORK_SLOT is job:
            WORK_SLOT = None
        raise


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler(["start", "help"], help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            task,
        )
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
