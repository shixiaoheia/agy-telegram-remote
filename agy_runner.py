"""One bounded, non-retrying agy execution path for both smoke tests and the bot."""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from settings import Settings

SMOKE_PROMPT = (
    "Reply with exactly: AGY ready. Do not use any tools, run commands, "
    "read files, modify files, or make network requests."
)


@dataclass
class Result:
    outcome: str
    text: str = ""
    detail: str = ""
    category: str = ""
    agy_status: str = ""
    exit_code: int | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    cleanup_ok: bool = True
    model: str = ""
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0
    conversation_id: str = ""
    num_turns: int = 0
    effort: str = ""
    mode: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def classify(message: str) -> str:
    """Heuristic diagnostic categories, not assertions about account state."""
    low = message.lower()
    # Explicit service eligibility denial is NOT an expired credential.
    # Do not classify a bare "location" or "country" as a rejection.
    if any(s in low for s in (
        "not eligible for antigravity",
        "not currently available in your location",
        "not available in your country",
        "unsupported region",
    )):
        return "eligibility"
    is_network = any(s in low for s in (
        "connection", "network", "dns", "timed out", "timeout", "deadline exceeded",
        "certificate", "unable to resolve", "dial tcp", "reset by peer", "broken pipe",
        "no route to host", "temporary failure", "ssl", "tls handshake"
    ))
    is_unambiguous_auth = any(s in low for s in (
        "authentication required", "not authenticated", "login required",
        "unauthenticated", "unauthorized", "invalid_grant", "token expired",
        "no credentials", "headlessauthrequired", "401", "re-authenticate",
        "sign in", "signin", "unauthorized_client"
    ))
    if is_network and not is_unambiguous_auth:
        return "network"
    if is_unambiguous_auth:
        return "auth"
    if any(s in low for s in ("quota", "rate limit", "resource_exhausted", "429")):
        return "quota"
    if "soft-denied" in low or "permissiondenied" in low or "permission_denied" in low:
        return "permission"
    if is_network:
        return "network"
    if any(s in low for s in ("oauth", "credentials")):
        return "auth"
    return "unknown"


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


def _validate_non_negative_int(value: object, field_name: str, max_val: int = 1_000_000_000) -> tuple[int, str | None]:
    if value is None:
        return 0, None
    if type(value) is not int:
        return 0, f"{field_name} 类型不合法（期望整数，实际为 {type(value).__name__}）"
    if value < 0 or value > max_val:
        return 0, f"{field_name} 数值超出有效范围 [0, {max_val}]：{value}"
    return value, None


def parse_result(stdout: bytes, stderr: bytes, exit_code: int) -> Result:
    """Only a complete JSON SUCCESS envelope with nonempty text is success."""
    diagnostics = stderr.decode("utf-8", errors="replace")
    category = classify(diagnostics)
    try:
        envelope = json.loads(
            stdout.decode("utf-8-sig"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        return Result(
            "invalid", detail="agy 未返回完整、有效的 JSON 结果；任务可能已产生修改。",
            category=category, exit_code=exit_code,
        )
    if not isinstance(envelope, dict):
        return Result("invalid", detail="agy 结果不是 JSON 对象。", exit_code=exit_code)
    status = envelope.get("status")
    response = envelope.get("response")
    raw_error = envelope.get("error")

    raw_conv = envelope.get("conversation_id")
    if raw_conv is not None and not isinstance(raw_conv, str):
        return Result(
            "invalid",
            detail="agy 返回的 conversation_id 字段不是字符串；任务可能已经执行，不能自动重跑。",
            category="protocol", agy_status=str(status)[:32] if status else "", exit_code=exit_code,
        )
    conversation_id = raw_conv or ""

    num_turns_val, err = _validate_non_negative_int(envelope.get("num_turns"), "num_turns", max_val=10_000_000)
    if err:
        return Result(
            "invalid",
            detail=f"agy 返回的数值字段不符合协议格式（{err}）；任务可能已经执行，不能自动重跑。",
            category="protocol", agy_status=str(status)[:32] if status else "", exit_code=exit_code,
        )
    num_turns = num_turns_val

    usage = envelope.get("usage")
    input_tokens, output_tokens, thinking_tokens, total_tokens = 0, 0, 0, 0
    if usage is not None:
        if not isinstance(usage, dict):
            return Result(
                "invalid",
                detail="agy 返回的 usage 字段不是字典对象；任务可能已经执行，不能自动重跑。",
                category="protocol", agy_status=str(status)[:32] if status else "", exit_code=exit_code,
            )
        for key in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens"):
            val, err = _validate_non_negative_int(usage.get(key), key)
            if err:
                return Result(
                    "invalid",
                    detail=f"agy 返回的数值字段不符合协议格式（{err}）；任务可能已经执行，不能自动重跑。",
                    category="protocol", agy_status=str(status)[:32] if status else "", exit_code=exit_code,
                )
            if key == "input_tokens":
                input_tokens = val
            elif key == "output_tokens":
                output_tokens = val
            elif key == "thinking_tokens":
                thinking_tokens = val
            elif key == "total_tokens":
                total_tokens = val

    if isinstance(raw_error, str):
        category = classify(raw_error + "\n" + diagnostics)
    if not isinstance(status, str) or status not in {
        "SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"
    }:
        return Result("invalid", detail="agy 结果缺少有效的最终状态。", exit_code=exit_code)
    if exit_code != 0 or status != "SUCCESS" or raw_error not in (None, ""):
        err_msg = str(raw_error).strip() if raw_error else ""
        detail = f"agy 返回异常状态 ({status})：{err_msg}" if err_msg else "agy 返回异常状态；已执行的操作不会自动撤销。"
        return Result(
            "error", detail=detail,
            category=category, agy_status=status[:32], exit_code=exit_code,
            conversation_id=conversation_id, num_turns=num_turns,
        )
    if not isinstance(response, str):
        return Result(
            "invalid", detail="agy 的 response 缺失或不是文本。",
            agy_status=status, exit_code=exit_code,
            conversation_id=conversation_id, num_turns=num_turns,
        )
    try:
        response.encode("utf-8")
    except UnicodeEncodeError:
        return Result("invalid", detail="agy 返回了无效的 Unicode 文本。", exit_code=exit_code)
    if "soft-denied" in diagnostics.lower():
        return Result(
            "permission", text=response.strip(),
            detail="检测到工具权限拒绝；请核对任务实际完成情况。",
            category="permission", agy_status=status, exit_code=exit_code,
            conversation_id=conversation_id, num_turns=num_turns,
            input_tokens=input_tokens, output_tokens=output_tokens,
            thinking_tokens=thinking_tokens, total_tokens=total_tokens,
        )
    if not response.strip():
        return Result(
            "no_text", detail="agy 已结束，但缺少最终文字回复；请先核对执行记录和工作目录。",
            agy_status=status, exit_code=exit_code,
            conversation_id=conversation_id, num_turns=num_turns,
            input_tokens=input_tokens, output_tokens=output_tokens,
            thinking_tokens=thinking_tokens, total_tokens=total_tokens,
        )
    return Result(
        "success", text=response.strip(), agy_status=status, exit_code=exit_code,
        conversation_id=conversation_id, num_turns=num_turns,
        input_tokens=input_tokens, output_tokens=output_tokens,
        thinking_tokens=thinking_tokens, total_tokens=total_tokens,
    )


def child_environment(home: Path, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    # Deliberately exclude BOT_TOKEN, PYTHONPATH, SSH_AUTH_SOCK and cloud keys.
    inherited = os.environ if inherited is None else inherited
    user = "root" if str(home) == "/root" else inherited.get("USER", "root")
    env = {
        "HOME": str(home), "USER": user, "LOGNAME": user,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
    }
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "all_proxy", "no_proxy"):
        if key in inherited:
            env[key] = inherited[key]
    return env


def build_command(settings: Settings, prompt: str, model: str | None = None,
                  conversation_id: str | None = None,
                  effort: str | None = None,
                  mode: str | None = None) -> list[str]:
    command = [
        str(settings.agy), "--print-timeout", f"{math.ceil(settings.timeout)}s",
        "--output-format", "json",
    ]
    if settings.skip_permissions:
        command.append("--dangerously-skip-permissions")
    if conversation_id:
        command.extend(["--conversation", conversation_id])
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    if mode:
        command.extend(["--mode", mode])
    return command + ["--print", prompt]


@dataclass
class Capture:
    data: bytes = b""
    total: int = 0


async def read_bounded(stream: asyncio.StreamReader, limit: int,
                       overflow: asyncio.Event) -> Capture:
    kept = bytearray()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if len(kept) < limit:
            kept.extend(chunk[:limit - len(kept)])
        if total > limit:
            overflow.set()
    return Capture(bytes(kept), total)


def live_group(pgid: int) -> bool:
    """Linux: ignore zombies; a live child matters even after its parent exited."""
    observed = False
    unreadable = False
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(tail[2]) == pgid:
                observed = True
                if tail[0] not in {"Z", "X"}:
                    return True
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            continue
        except PermissionError:
            unreadable = True
    if observed and not unreadable:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Only zombies were observed, or /proc could not be read completely.
    return unreadable or not observed


def group_signal(pgid: int, sig: int) -> None:
    if pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError("Refusing unsafe process group")
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


async def cleanup_group(process: asyncio.subprocess.Process, grace: float = 3.0,
                        kill_grace: float = 2.0) -> bool:
    pgid = process.pid
    if live_group(pgid):
        group_signal(pgid, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + grace
        while live_group(pgid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.03)
    if live_group(pgid):
        group_signal(pgid, signal.SIGKILL)
        deadline = asyncio.get_running_loop().time() + kill_grace
        while live_group(pgid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.03)
    return not live_group(pgid)


async def parent_exit(process: asyncio.subprocess.Process) -> None:
    # Process.wait() can wait on inherited pipes after the parent has exited.
    while process.returncode is None:
        await asyncio.sleep(0.025)


class Runner:
    def __init__(self, settings: Settings, *, outer_grace: float = 5.0,
                 terminate_grace: float = 3.0):
        self.settings = settings
        self.outer_grace = outer_grace
        self.terminate_grace = terminate_grace
        self.blocked = False

    async def run(self, prompt: str, cancel: asyncio.Event,
                  model: str | None = None, conversation_id: str | None = None,
                  effort: str | None = None, mode: str | None = None) -> Result:
        selected_model = model or self.settings.model or ""
        start_time = asyncio.get_running_loop().time()
        if self.blocked:
            return Result("cleanup_failed", detail="上次进程清理未确认完成；请重启服务后检查。",
                          cleanup_ok=False, model=selected_model, effort=effort or "", mode=mode or "")
        if cancel.is_set():
            return Result("cancelled", detail="任务在启动前已取消。", model=selected_model,
                          effort=effort or "", mode=mode or "")
        if not prompt.strip() or "\x00" in prompt or len(prompt) > self.settings.max_prompt:
            return Result("error", detail="任务为空、过长或含非法字符。", category="input",
                          model=selected_model, effort=effort or "", mode=mode or "")
        process = None
        readers: list[asyncio.Task] = []
        watchers: list[asyncio.Task] = []
        result = Result("error", detail="agy 启动或执行异常。", category="process",
                        model=selected_model, effort=effort or "", mode=mode or "")
        task_cancelled = False
        captured = [Capture(), Capture()]
        cleanup_ok = True
        try:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *build_command(self.settings, prompt, model=selected_model or None,
                               conversation_id=conversation_id or None,
                               effort=effort or None, mode=mode or None),
                cwd=self.settings.workspace,
                env=child_environment(self.settings.home),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            ))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Do not lose a child spawned between cancellation and exec completion.
                task_cancelled = True
                cancel.set()
                process = await spawn
            assert process.stdout is not None and process.stderr is not None
            overflow = asyncio.Event()
            readers = [
                asyncio.create_task(read_bounded(process.stdout, self.settings.max_output, overflow)),
                asyncio.create_task(read_bounded(process.stderr, min(self.settings.max_output, 262144), overflow)),
            ]
            watchers = [
                asyncio.create_task(parent_exit(process)),
                asyncio.create_task(cancel.wait()),
                asyncio.create_task(overflow.wait()),
            ]
            done, _ = await asyncio.wait(
                watchers, timeout=self.settings.timeout + self.outer_grace,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel.is_set():
                result = Result("cancelled", detail="已请求取消；此前完成的操作不会自动撤销。")
            elif overflow.is_set():
                result = Result("output_limit", detail="输出超过保留上限，任务已停止；不解析截断数据。")
            elif not done:
                result = Result("timed_out", detail="任务超时，已请求停止；请核对已有修改。")
            else:
                result = Result("pending")
        except asyncio.CancelledError:
            task_cancelled = True
            result = Result("cancelled", detail="服务停止或任务被取消。")
        except (OSError, RuntimeError):
            result = Result("error", detail="无法启动或管理 agy；请检查程序、目录与权限。",
                            category="process")
        finally:
            if process is not None:
                # A shielded cleanup remains alive even if the caller is cancelled.
                cleanup = asyncio.create_task(cleanup_group(
                    process, self.terminate_grace, max(0.3, self.terminate_grace)))
                try:
                    cleanup_ok = await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    task_cancelled = True
                    cleanup_ok = await cleanup
                except (OSError, RuntimeError):
                    cleanup_ok = False
                if readers:
                    gather = asyncio.gather(*readers, return_exceptions=True)
                    try:
                        outputs = await asyncio.wait_for(asyncio.shield(gather), 2.0)
                        for index, output in enumerate(outputs):
                            if isinstance(output, Capture):
                                captured[index] = output
                            else:
                                cleanup_ok = False
                    except asyncio.TimeoutError:
                        # A setsid() descendant may retain pipes outside our group.
                        cleanup_ok = False
                        for reader in readers:
                            reader.cancel()
                        await asyncio.gather(*readers, return_exceptions=True)
                        await gather
                try:
                    await asyncio.wait_for(process.wait(), 1.0)
                except asyncio.TimeoutError:
                    cleanup_ok = False
                if not cleanup_ok:
                    # asyncio exposes no public Process.close(); this is only an
                    # emergency pipe-close fallback, never the termination logic.
                    transport = getattr(process, "_transport", None)
                    if transport is not None:
                        transport.close()
            for watcher in watchers:
                watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)

        if not cleanup_ok:
            self.blocked = True
            result = Result("cleanup_failed", detail="未能确认全部子进程与管道已清理；已暂停接收新任务。",
                            cleanup_ok=False)
        elif result.outcome == "pending" and process is not None:
            if (captured[0].total > self.settings.max_output
                    or captured[1].total > min(self.settings.max_output, 262144)):
                result = Result("output_limit", detail="输出超过上限；没有解析或转发截断数据。")
            else:
                result = parse_result(captured[0].data, captured[1].data, process.returncode or 0)
        result.stdout_bytes = captured[0].total
        result.stderr_bytes = captured[1].total
        if selected_model:
            result.model = selected_model
        if effort:
            result.effort = effort
        if mode:
            result.mode = mode
        if process is not None:
            result.exit_code = process.returncode
        if task_cancelled and result.outcome == "success":
            result = Result("cancelled", detail="任务已取消，请核对已有修改。", model=selected_model)
        result.duration_seconds = round(asyncio.get_running_loop().time() - start_time, 1)
        return result
