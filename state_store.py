"""Bounded per-user last results and a durable Telegram update watermark."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from settings import MODEL_RE, check_no_symlink, resolve_model

HISTORY_ITEMS = 10


def _ensure_private_dir(directory: Path, message: str, *, parents: bool = False) -> None:
    check_no_symlink(directory)
    directory.mkdir(mode=0o700, parents=parents, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(message)


def _valid_timestamp(stamp: object, *, allow_zero: bool = True) -> bool:
    return (type(stamp) in {int, float} and math.isfinite(stamp)
            and (0 <= stamp if allow_zero else 0 < stamp) and stamp <= 253402300799)


def atomic_json(path: Path, value: Any) -> None:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".atomic-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)  # replaces a link itself, never its target
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path, maximum: int = 1048576) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Invalid state file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Oversized state file")
        return json.loads(data.decode("utf-8"))
    finally:
        os.close(fd)


class Store:
    def __init__(self, directory: Path, allowed: frozenset[int], max_reply: int,
                 retention_days: int, token: str = ""):
        _ensure_private_dir(
            directory, "State directory must be owned by this user and private (0700)",
            parents=True,
        )
        self.directory = directory
        self.base_allowed = frozenset(allowed)
        self.allowed = set(allowed)
        self.max_reply = max_reply
        self.retention = retention_days * 86400
        self.token = token
        self.lock_fd: int | None = None
        for uid in self.get_extra_whitelist():
            self.allowed.add(uid)

    def lock(self) -> None:
        fd = os.open(self.directory / "service.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Invalid lock file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.lock_fd = fd

    def close(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def load_queue(self, max_prompt: int) -> list[tuple[int, int, str]]:
        try:
            data = read_json(self.directory / "waiting-queue.json", 18 * max_prompt + 4096)
        except FileNotFoundError:
            return []
        if not isinstance(data, dict) or type(data.get("saved_at")) not in {int, float}:
            raise ValueError("Invalid queue file")
        entries = data.get("entries")
        if not isinstance(entries, list) or len(entries) > 3:
            raise ValueError("Invalid queue file")
        if not 0 <= time.time() - data["saved_at"] <= self.retention:
            (self.directory / "waiting-queue.json").unlink(missing_ok=True)
            return []
        result = []
        for item in entries:
            if (not isinstance(item, list) or len(item) != 3 or type(item[0]) is not int
                    or type(item[1]) is not int or item[0] != item[1]
                    or not isinstance(item[2], str) or not item[2]
                    or len(item[2]) > max_prompt or "\x00" in item[2]):
                raise ValueError("Invalid queue entry")
            if item[0] in self.allowed:
                result.append(tuple(item))
        return result

    def save_queue(self, entries: list[tuple[int, int, str]]) -> None:
        if len(entries) > 3:
            raise ValueError("Queue is full")
        atomic_json(self.directory / "waiting-queue.json", {"saved_at": time.time(), "entries": entries})

    def _emergency_path(self, home: Path, user: int) -> Path:
        self._path(user)  # enforce the current whitelist
        directory = home / ".agy-telegram-recovery"
        _ensure_private_dir(directory, "Recovery directory must be private")
        return directory / f"result-{user}.json"

    def save_emergency(self, home: Path, user: int, record: dict) -> None:
        atomic_json(self._emergency_path(home, user), self.prepare_record(user, record))

    def load_emergency(self, home: Path, user: int) -> dict | None:
        try:
            value = read_json(self._emergency_path(home, user), self.max_reply * 12 + 65536)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("user_id") != user:
            raise ValueError("Invalid recovery record")
        return value

    def clear_emergency(self, home: Path, user: int) -> None:
        self._emergency_path(home, user).unlink(missing_ok=True)

    def _path(self, user: int, kind: str = "last") -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"{kind}-{user}.json"

    def _read_user_record(self, user: int, kind: str, maximum: int = 1024) -> dict | None:
        try:
            value = read_json(self._path(user, kind), maximum)
        except FileNotFoundError:
            return None
        return value if isinstance(value, dict) and value.get("user_id") == user else None

    def redact(self, value: str) -> str:
        if self.token:
            value = value.replace(self.token, "[BOT_TOKEN_REDACTED]")
        return value

    def prepare_record(self, user: int, value: dict) -> dict:
        """Bound and redact a record before any durable or temporary retention."""
        if type(user) is not int or user <= 0:
            raise ValueError("Invalid user ID")
        record = dict(value)
        record["user_id"] = user
        record["updated_at"] = time.time()
        if "title" in record:
            record["title"] = self.redact(" ".join(str(record["title"]).split()))[:160]
        if "metadata_warning" in record:
            record["metadata_warning"] = self.redact(str(record["metadata_warning"]))[:500]
        for key, limit in (("attachment_summary", 400), ("execution_summary", 1200)):
            if key in record:
                record[key] = self.redact(str(record[key]))[:limit]
        for key in ("text", "detail"):
            text = self.redact(str(record.get(key, "")))
            if len(text) > self.max_reply:
                suffix = "\n\n（回复超过保存上限，已截断；/last 也只能取回这份截断副本。）"
                text = text[:self.max_reply - len(suffix)] + suffix
                record[key + "_truncated"] = True
            record[key] = text
        # No prompt, raw stdout/stderr, or credentials are stored here.
        record = {key: record[key] for key in (
            "user_id", "updated_at", "job_id", "outcome", "text", "detail", "text_truncated", "detail_truncated",
            "category", "agy_status", "exit_code", "stdout_bytes", "stderr_bytes",
            "cleanup_ok", "delivery", "model", "effort", "mode", "duration_seconds",
            "input_tokens", "output_tokens", "thinking_tokens", "total_tokens",
            "conversation_id", "num_turns",
            "session_tokens", "session_token_reminder", "model_fallback_from",
            "title", "metadata_warning", "attachment_summary", "execution_summary",
        ) if key in record}
        return record

    def save(self, user: int, value: dict) -> dict:
        record = self.prepare_record(user, value)
        atomic_json(self._path(user), record)
        return record

    def get_language(self, user: int) -> str:
        path = self._path(user, "language")
        try:
            value = read_json(path, 1024)
        except (OSError, ValueError):
            return "zh"
        if isinstance(value, dict) and value.get("user_id") == user and value.get("language") in {"zh", "en"}:
            return value["language"]
        return "zh"

    def set_language(self, user: int, language: str) -> None:
        path = self._path(user, "language")
        if language not in {"zh", "en"}:
            raise ValueError("Invalid language")
        atomic_json(path, {"user_id": user, "language": language, "updated_at": time.time()})

    def get_model(self, user: int) -> str | None:
        value = self._read_user_record(user, "model")
        model = value.get("model") if value else None
        if isinstance(model, str) and MODEL_RE.fullmatch(model):
            return model if value.get("exact") is True else resolve_model(model)
        return None

    def set_model(self, user: int, model: str | None, *, exact: bool = False) -> None:
        path = self._path(user, "model")
        if model is None:
            path.unlink(missing_ok=True)
            return
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise ValueError("Invalid model name")
        atomic_json(path, {"user_id": user, "model": model, "exact": exact, "updated_at": time.time()})

    def get_model_catalog(self) -> dict:
        """A separate snapshot: historical probe results are never a model list."""
        try:
            value = read_json(self.directory / "model-catalog.json", 16384)
        except FileNotFoundError:
            return {"checked_at": 0.0, "models": []}
        if not isinstance(value, dict):
            raise ValueError("Invalid model catalog")
        stamp, models = value.get("checked_at"), value.get("models")
        if not _valid_timestamp(stamp, allow_zero=False):
            raise ValueError("Invalid model catalog timestamp")
        self._validate_catalog(models)
        return {"checked_at": float(stamp), "models": list(models)}

    @staticmethod
    def _validate_catalog(models) -> None:
        if (not isinstance(models, (list, tuple)) or not 1 <= len(models) <= 128
                or any(not isinstance(model, str) or not MODEL_RE.fullmatch(model) for model in models)
                or len(set(models)) != len(models)):
            raise ValueError("Invalid model catalog entries")

    def set_model_catalog(self, models) -> None:
        self._validate_catalog(models)
        atomic_json(self.directory / "model-catalog.json",
                    {"checked_at": time.time(), "models": list(models)})

    def get_model_health(self) -> dict:
        """Return the bounded, account-wide result of explicit model probes."""
        try:
            value = read_json(self.directory / "model-health.json", 32768)
        except FileNotFoundError:
            return {"checked_at": 0.0, "models": {}}
        if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
            return {"checked_at": 0.0, "models": {}}
        checked_at = value.get("checked_at")
        if not _valid_timestamp(checked_at):
            return {"checked_at": 0.0, "models": {}}
        models: dict[str, dict] = {}
        for model, result in value["models"].items():
            if (isinstance(model, str) and MODEL_RE.fullmatch(model)
                    and isinstance(result, dict)
                    and result.get("outcome") in {"success", "error", "timed_out", "invalid"}):
                category = result.get("category", "unknown")
                stamp = result.get("checked_at", checked_at)
                models[model] = {
                    "outcome": result["outcome"],
                    "category": category if isinstance(category, str) and len(category) <= 32 else "unknown",
                    "checked_at": float(stamp) if _valid_timestamp(stamp) else 0.0,
                }
        return {"checked_at": float(checked_at), "models": models}

    def set_model_health(self, models: dict[str, dict]) -> None:
        clean: dict[str, dict] = {}
        for model, result in models.items():
            if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
                raise ValueError("Invalid model health key")
            if not isinstance(result, dict) or result.get("outcome") not in {
                "success", "error", "timed_out", "invalid"
            }:
                raise ValueError("Invalid model health result")
            category = result.get("category", "unknown")
            stamp = result.get("checked_at", time.time())
            if not _valid_timestamp(stamp):
                raise ValueError("Invalid model health timestamp")
            clean[model] = {
                "outcome": result["outcome"],
                "category": category if isinstance(category, str) and len(category) <= 32 else "unknown",
                "checked_at": float(stamp),
            }
        atomic_json(self.directory / "model-health.json", {"checked_at": time.time(), "models": clean})

    def _get_choice(self, user: int, kind: str, choices: tuple[str, ...]) -> str | None:
        value = self._read_user_record(user, kind)
        choice = value.get(kind) if value else None
        return choice if isinstance(choice, str) and choice in choices else None

    def _set_choice(self, user: int, kind: str, choice: str | None, choices: tuple[str, ...]) -> None:
        path = self._path(user, kind)
        if choice is None:
            path.unlink(missing_ok=True)
            return
        if choice not in choices:
            raise ValueError(f"Invalid {kind} setting")
        atomic_json(path, {"user_id": user, kind: choice, "updated_at": time.time()})

    def get_effort(self, user: int) -> str | None:
        return self._get_choice(user, "effort", ("low", "medium", "high"))

    def set_effort(self, user: int, effort: str | None) -> None:
        self._set_choice(user, "effort", effort, ("low", "medium", "high"))

    def get_mode(self, user: int) -> str | None:
        return self._get_choice(user, "mode", ("plan", "accept-edits"))

    def set_mode(self, user: int, mode: str | None) -> None:
        self._set_choice(user, "mode", mode, ("plan", "accept-edits"))

    def get_extra_whitelist(self) -> list[int]:
        path = self.directory / "whitelist.json"
        try:
            value = read_json(path, 8192)
        except FileNotFoundError:
            return []
        if isinstance(value, list) and all(isinstance(x, int) and x > 0 for x in value):
            return value
        return []

    def add_whitelist(self, user: int) -> bool:
        if not isinstance(user, int) or user <= 0:
            raise ValueError("Invalid user ID")
        current = self.get_extra_whitelist()
        if user in current or user in self.base_allowed:
            return False
        current.append(user)
        atomic_json(self.directory / "whitelist.json", current)
        self.allowed.add(user)
        return True

    def remove_whitelist(self, user: int) -> bool:
        if not isinstance(user, int) or user <= 0:
            raise ValueError("Invalid user ID")
        current = self.get_extra_whitelist()
        if user not in current:
            return False
        current.remove(user)
        atomic_json(self.directory / "whitelist.json", current)
        if user not in self.base_allowed:
            self.allowed.discard(user)
        return True

    def get_conversation(self, user: int) -> dict | None:
        value = self._read_user_record(user, "conv", 2048)
        cid = value.get("conversation_id") if value else None
        if isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9._-]{2,128}", cid):
            return value
        return None

    def set_conversation(self, user: int, conversation_id: str | None, num_turns: int = 1,
                         session_tokens: int = 0) -> None:
        path = self._path(user, "conv")
        if not conversation_id:
            path.unlink(missing_ok=True)
            return
        if not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{2,128}", conversation_id):
            raise ValueError("Invalid conversation ID")
        atomic_json(path, {
            "user_id": user,
            "conversation_id": conversation_id,
            "num_turns": num_turns,
            "session_tokens": max(0, int(session_tokens or 0)),
            "updated_at": time.time(),
        })

    def reset_conversation(self, user: int) -> None:
        self.set_conversation(user, None)

    def get_usage(self, user: int) -> dict:
        value = self._read_user_record(user, "usage", 2048)
        return value or {
            "user_id": user, "total_input_tokens": 0, "total_output_tokens": 0,
            "total_thinking_tokens": 0, "total_turns": 0,
        }

    def record_usage(self, user: int, input_tokens: int, output_tokens: int,
                     thinking_tokens: int = 0) -> dict:
        curr = self.get_usage(user)
        new_usage = {
            "user_id": user,
            "total_input_tokens": curr.get("total_input_tokens", 0) + max(0, input_tokens),
            "total_output_tokens": curr.get("total_output_tokens", 0) + max(0, output_tokens),
            "total_thinking_tokens": curr.get("total_thinking_tokens", 0) + max(0, thinking_tokens),
            "total_turns": curr.get("total_turns", 0) + 1,
            "updated_at": time.time(),
        }
        atomic_json(self._path(user, "usage"), new_usage)
        return new_usage

    def load(self, user: int) -> dict | None:
        path = self._path(user)
        try:
            value = read_json(path, 12 * self.max_reply + 16384)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("user_id") != user:
            raise ValueError("Invalid result record")
        updated = value.get("updated_at")
        if not isinstance(updated, (int, float)):
            raise ValueError("Invalid result timestamp")
        if time.time() - updated > self.retention:
            path.unlink(missing_ok=True)
            return None
        return value

    def _history_maximum(self) -> int:
        # Each result has two capped text fields (up to six JSON bytes per
        # character), plus metadata. History retains ten complete results,
        # rather than sharing the byte allowance of a single result.
        return HISTORY_ITEMS * (12 * self.max_reply + 16384) + 65536

    def _write_history(self, user: int, items: list[dict]) -> None:
        value = {"user_id": user, "items": items[:HISTORY_ITEMS]}
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size > self._history_maximum():
            raise ValueError("Oversized history record")
        atomic_json(self._path(user, "history"), value)

    def save_history(self, user: int, title: str, record: dict) -> None:
        """Keep a small, private, per-user result history for /history."""
        title = self.redact(" ".join(str(title).split()))[:160] or "未命名任务"
        try:
            value = read_json(self._path(user, "history"), self._history_maximum())
            items = value.get("items", []) if isinstance(value, dict) else []
        except FileNotFoundError:
            items = []
        if not isinstance(items, list):
            items = []
        item = dict(record)
        item["title"] = title
        item["user_id"] = user
        # Newest first. Keep full stored records, but never an unbounded history.
        items = [item] + [entry for entry in items
                          if isinstance(entry, dict) and entry.get("job_id") != item.get("job_id")]
        self._write_history(user, items)

    def history(self, user: int) -> list[dict]:
        path = self._path(user, "history")
        try:
            value = read_json(path, self._history_maximum())
        except FileNotFoundError:
            return []
        if not isinstance(value, dict) or value.get("user_id") != user:
            raise ValueError("Invalid history record")
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("Invalid history record")
        now = time.time()
        valid = [entry for entry in items if isinstance(entry, dict)
                 and entry.get("user_id") == user
                 and isinstance(entry.get("updated_at"), (int, float))
                 and now - entry["updated_at"] <= self.retention]
        if len(valid) != len(items) or len(valid) > HISTORY_ITEMS:
            self._write_history(user, valid)
        return valid[:HISTORY_ITEMS]

    def history_item(self, user: int, job_id: str) -> dict | None:
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{12}", job_id):
            return None
        return next((item for item in self.history(user) if item.get("job_id") == job_id), None)

    def maintain(self) -> None:
        """Runs at startup and periodically: expire or remove non-allowed records."""
        for path in self.directory.iterdir():
            match = re.fullmatch(
                r"(last|history|model|language|conv|usage|effort|mode)-([0-9]+)\.json",
                path.name,
            )
            if not match:
                continue
            kind, user = match[1], int(match[2])
            if path.is_symlink():
                raise ValueError("Linked state record")
            if user not in self.allowed:
                path.unlink()
            elif kind == "last":
                self.load(user)
            elif kind == "history":
                self.history(user)

    def recover_interrupted(self) -> None:
        self.maintain()
        for user in self.allowed:
            record = self.load(user)
            if record and record.get("outcome") == "running":
                record.update(outcome="interrupted", delivery="unknown",
                              detail="服务在上次任务期间中断；任务可能产生了修改，没有自动重跑。")
                record = self.save(user, record)
            if record and record.get("outcome") == "interrupted":
                existing = next((entry for entry in self.history(user)
                                 if entry.get("job_id") == record.get("job_id")), None)
                if existing is None or existing.get("outcome") == "running":
                    self.save_history(user, record.get("title") or "服务中断的任务", record)

    def offset(self) -> int:
        try:
            value = read_json(self.directory / "offset.json", 256)
        except FileNotFoundError:
            return 0
        if type(value) is not int or value < 0:
            raise ValueError("Invalid Telegram update watermark")
        return value

    def save_offset(self, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("Invalid update watermark")
        atomic_json(self.directory / "offset.json", value)

