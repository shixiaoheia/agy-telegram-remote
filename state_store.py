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
        self.allowed = allowed
        self.max_reply = max_reply
        self.retention = retention_days * 86400
        self.token = token
        self.lock_fd: int | None = None

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
            "cleanup_ok", "delivery",
        ) if key in record}
        atomic_json(self._path(user), record)
        return record

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

    def maintain(self) -> None:
        """Runs at startup and periodically: expire or remove non-allowed records."""
        for path in self.directory.iterdir():
            match = re.fullmatch(r"last-([0-9]+)\.json", path.name)
            if not match:
                continue
            user = int(match[1])
            if path.is_symlink():
                raise ValueError("Linked state record")
            if user not in self.allowed:
                path.unlink()
                continue
            self.load(user)

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
