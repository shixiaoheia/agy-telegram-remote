"""Bounded per-user last results and a durable Telegram update watermark."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from settings import check_no_symlink


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
        check_no_symlink(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("State directory must be owned by this user and private (0700)")
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

    def _path(self, user: int) -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"last-{user}.json"

    def redact(self, value: str) -> str:
        if self.token:
            value = value.replace(self.token, "[BOT_TOKEN_REDACTED]")
        return value

    def save(self, user: int, value: dict) -> dict:
        record = dict(value)
        record["user_id"] = user
        record["updated_at"] = time.time()
        for key in ("text", "detail"):
            text = self.redact(str(record.get(key, "")))
            if len(text) > self.max_reply:
                suffix = "\n\n（回复超过保存上限，已截断；/last 也只能取回这份截断副本。）"
                text = text[:self.max_reply - len(suffix)] + suffix
            record[key] = text
        # No prompt, raw stdout/stderr, or credentials are stored here.
        record = {key: record[key] for key in (
            "user_id", "updated_at", "job_id", "outcome", "text", "detail",
            "category", "agy_status", "exit_code", "stdout_bytes", "stderr_bytes",
            "cleanup_ok", "delivery", "model", "duration_seconds",
            "input_tokens", "output_tokens", "thinking_tokens", "total_tokens",
            "conversation_id", "num_turns",
        ) if key in record}
        atomic_json(self._path(user), record)
        return record

    def _model_path(self, user: int) -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"model-{user}.json"

    def get_model(self, user: int) -> str | None:
        path = self._model_path(user)
        try:
            value = read_json(path, 1024)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("user_id") != user:
            return None
        model = value.get("model")
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._-]{2,64}", model):
            return model
        return None

    def set_model(self, user: int, model: str | None) -> None:
        path = self._model_path(user)
        if model is None:
            path.unlink(missing_ok=True)
            return
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", model):
            raise ValueError("Invalid model name")
        atomic_json(path, {"user_id": user, "model": model, "updated_at": time.time()})

    def _model_health_path(self) -> Path:
        return self.directory / "model-health.json"

    def get_model_health(self) -> dict:
        """Return the bounded, account-wide result of explicit model probes."""
        try:
            value = read_json(self._model_health_path(), 32768)
        except FileNotFoundError:
            return {"checked_at": 0.0, "models": {}}
        if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
            return {"checked_at": 0.0, "models": {}}
        checked_at = value.get("checked_at")
        if not isinstance(checked_at, (int, float)) or checked_at < 0:
            return {"checked_at": 0.0, "models": {}}
        models: dict[str, dict] = {}
        for model, result in value["models"].items():
            if (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._-]{2,64}", model)
                    and isinstance(result, dict)
                    and result.get("outcome") in {"success", "error", "timed_out", "invalid"}):
                category = result.get("category", "unknown")
                models[model] = {
                    "outcome": result["outcome"],
                    "category": category if isinstance(category, str) and len(category) <= 32 else "unknown",
                }
        return {"checked_at": float(checked_at), "models": models}

    def set_model_health(self, models: dict[str, dict]) -> None:
        clean: dict[str, dict] = {}
        for model, result in models.items():
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", model):
                raise ValueError("Invalid model health key")
            if not isinstance(result, dict) or result.get("outcome") not in {
                "success", "error", "timed_out", "invalid"
            }:
                raise ValueError("Invalid model health result")
            category = result.get("category", "unknown")
            clean[model] = {
                "outcome": result["outcome"],
                "category": category if isinstance(category, str) and len(category) <= 32 else "unknown",
            }
        atomic_json(self._model_health_path(), {"checked_at": time.time(), "models": clean})

    def _effort_path(self, user: int) -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"effort-{user}.json"

    def get_effort(self, user: int) -> str | None:
        path = self._effort_path(user)
        try:
            value = read_json(path, 1024)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("user_id") != user:
            return None
        effort = value.get("effort")
        if isinstance(effort, str) and effort in ("low", "medium", "high"):
            return effort
        return None

    def set_effort(self, user: int, effort: str | None) -> None:
        path = self._effort_path(user)
        if effort is None:
            path.unlink(missing_ok=True)
            return
        if effort not in ("low", "medium", "high"):
            raise ValueError("Invalid effort setting")
        atomic_json(path, {"user_id": user, "effort": effort, "updated_at": time.time()})

    def _mode_path(self, user: int) -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"mode-{user}.json"

    def get_mode(self, user: int) -> str | None:
        path = self._mode_path(user)
        try:
            value = read_json(path, 1024)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("user_id") != user:
            return None
        mode = value.get("mode")
        if isinstance(mode, str) and mode in ("plan", "accept-edits"):
            return mode
        return None

    def set_mode(self, user: int, mode: str | None) -> None:
        path = self._mode_path(user)
        if mode is None:
            path.unlink(missing_ok=True)
            return
        if mode not in ("plan", "accept-edits"):
            raise ValueError("Invalid mode setting")
        atomic_json(path, {"user_id": user, "mode": mode, "updated_at": time.time()})

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

    def _conv_path(self, user: int) -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"conv-{user}.json"

    def get_conversation(self, user: int) -> dict | None:
        path = self._conv_path(user)
        try:
            value = read_json(path, 2048)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("user_id") != user:
            return None
        cid = value.get("conversation_id")
        if isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9._-]{2,128}", cid):
            return value
        return None

    def set_conversation(self, user: int, conversation_id: str | None, num_turns: int = 1) -> None:
        path = self._conv_path(user)
        if not conversation_id:
            path.unlink(missing_ok=True)
            return
        if not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{2,128}", conversation_id):
            raise ValueError("Invalid conversation ID")
        atomic_json(path, {
            "user_id": user,
            "conversation_id": conversation_id,
            "num_turns": num_turns,
            "updated_at": time.time(),
        })

    def reset_conversation(self, user: int) -> None:
        self.set_conversation(user, None)

    def _usage_path(self, user: int) -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"usage-{user}.json"

    def get_usage(self, user: int) -> dict:
        path = self._usage_path(user)
        try:
            value = read_json(path, 2048)
        except FileNotFoundError:
            return {
                "user_id": user, "total_input_tokens": 0, "total_output_tokens": 0,
                "total_thinking_tokens": 0, "total_turns": 0,
            }
        if not isinstance(value, dict) or value.get("user_id") != user:
            return {
                "user_id": user, "total_input_tokens": 0, "total_output_tokens": 0,
                "total_thinking_tokens": 0, "total_turns": 0,
            }
        return value

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
        atomic_json(self._usage_path(user), new_usage)
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

    def _history_path(self, user: int) -> Path:
        if type(user) is not int or user not in self.allowed:
            raise ValueError("User is not allowed")
        return self.directory / f"history-{user}.json"

    def save_history(self, user: int, title: str, record: dict) -> None:
        """Keep a small, private, per-user result history for /history."""
        title = self.redact(" ".join(str(title).split()))[:160] or "未命名任务"
        try:
            value = read_json(self._history_path(user), 12 * self.max_reply + 65536)
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
        atomic_json(self._history_path(user), {"user_id": user, "items": items[:10]})

    def history(self, user: int) -> list[dict]:
        path = self._history_path(user)
        try:
            value = read_json(path, 12 * self.max_reply + 65536)
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
        if len(valid) != len(items):
            atomic_json(path, {"user_id": user, "items": valid[:10]})
        return valid[:10]

    def history_item(self, user: int, job_id: str) -> dict | None:
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{12}", job_id):
            return None
        return next((item for item in self.history(user) if item.get("job_id") == job_id), None)

    def maintain(self) -> None:
        """Runs at startup and periodically: expire or remove non-allowed records."""
        for path in self.directory.iterdir():
            match_last = re.fullmatch(r"last-([0-9]+)\.json", path.name)
            if match_last:
                user = int(match_last[1])
                if path.is_symlink():
                    raise ValueError("Linked state record")
                if user not in self.allowed:
                    path.unlink()
                    continue
                self.load(user)
                continue
            match_history = re.fullmatch(r"history-([0-9]+)\.json", path.name)
            if match_history:
                user = int(match_history[1])
                if path.is_symlink():
                    raise ValueError("Linked state record")
                if user not in self.allowed:
                    path.unlink()
                    continue
                self.history(user)
                continue
            match_model = re.fullmatch(r"model-([0-9]+)\.json", path.name)
            if match_model:
                user = int(match_model[1])
                if path.is_symlink():
                    raise ValueError("Linked state record")
                if user not in self.allowed:
                    path.unlink()
                continue
            match_conv = re.fullmatch(r"conv-([0-9]+)\.json", path.name)
            if match_conv:
                user = int(match_conv[1])
                if path.is_symlink():
                    raise ValueError("Linked state record")
                if user not in self.allowed:
                    path.unlink()
                continue
            match_usage = re.fullmatch(r"usage-([0-9]+)\.json", path.name)
            if match_usage:
                user = int(match_usage[1])
                if path.is_symlink():
                    raise ValueError("Linked state record")
                if user not in self.allowed:
                    path.unlink()
                continue
            match_effort = re.fullmatch(r"effort-([0-9]+)\.json", path.name)
            if match_effort:
                user = int(match_effort[1])
                if path.is_symlink():
                    raise ValueError("Linked state record")
                if user not in self.allowed:
                    path.unlink()
                continue
            match_mode = re.fullmatch(r"mode-([0-9]+)\.json", path.name)
            if match_mode:
                user = int(match_mode[1])
                if path.is_symlink():
                    raise ValueError("Linked state record")
                if user not in self.allowed:
                    path.unlink()

    def recover_interrupted(self) -> None:
        self.maintain()
        for user in self.allowed:
            record = self.load(user)
            if record and record.get("outcome") == "running":
                record.update(outcome="interrupted", delivery="unknown",
                              detail="服务在上次任务期间中断；任务可能产生了修改，没有自动重跑。")
                self.save(user, record)

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
