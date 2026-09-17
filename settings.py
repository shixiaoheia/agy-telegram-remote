"""Strict, non-executable configuration. Python 3.10+, standard library only."""
from __future__ import annotations

import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

TOKEN_RE = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}\Z")
PATH_RE = re.compile(r"/[A-Za-z0-9_./-]+\Z")
MODEL_RE = re.compile(r"[A-Za-z0-9._-]{2,64}\Z")

# Canonical catalog of all official models supported by agy CLI (from `agy models`)
OFFICIAL_MODELS: tuple[tuple[str, str, str], ...] = (
    ("Gemini 3.8 Flash (最新极速)", "gemini-3.8-flash-high", "High 思考 (推荐)"),
    ("Gemini 3.8 Flash (最新极速)", "gemini-3.8-flash-medium", "Medium 思考"),
    ("Gemini 3.8 Flash (最新极速)", "gemini-3.8-flash-low", "Low 思考"),
    ("Gemini 3.7 Flash", "gemini-3.7-flash-high", "High 思考"),
    ("Gemini 3.7 Flash", "gemini-3.7-flash-medium", "Medium 思考"),
    ("Gemini 3.7 Flash", "gemini-3.7-flash-low", "Low 思考"),
    ("Gemini 3.6 Flash", "gemini-3.6-flash-high", "High 思考"),
    ("Gemini 3.6 Flash", "gemini-3.6-flash-medium", "Medium 思考"),
    ("Gemini 3.6 Flash", "gemini-3.6-flash-low", "Low 思考"),
    ("Gemini 3.1 Pro (深度推理)", "gemini-3.1-pro-high", "High 深度推理 (推荐)"),
    ("Gemini 3.1 Pro (深度推理)", "gemini-3.1-pro-low", "Low 推理"),
    ("Anthropic Claude", "claude-sonnet-4-6", "Sonnet 4.6 (Thinking)"),
    ("Anthropic Claude", "claude-opus-4-6-thinking", "Opus 4.6 (Thinking)"),
    ("开源模型", "gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
)

MODEL_ALIASES: dict[str, str] = {
    "3.8": "gemini-3.8-flash-high",
    "3.8-high": "gemini-3.8-flash-high",
    "3.8-med": "gemini-3.8-flash-medium",
    "3.8-low": "gemini-3.8-flash-low",
    "flash": "gemini-3.8-flash-high",
    "3.7": "gemini-3.7-flash-high",
    "3.7-high": "gemini-3.7-flash-high",
    "3.7-med": "gemini-3.7-flash-medium",
    "3.7-low": "gemini-3.7-flash-low",
    "3.6": "gemini-3.6-flash-high",
    "3.6-high": "gemini-3.6-flash-high",
    "3.6-med": "gemini-3.6-flash-medium",
    "3.6-low": "gemini-3.6-flash-low",
    "3.1": "gemini-3.1-pro-high",
    "pro": "gemini-3.1-pro-high",
    "pro-high": "gemini-3.1-pro-high",
    "pro-low": "gemini-3.1-pro-low",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6-thinking",
    "gpt": "gpt-oss-120b-medium",
    "120b": "gpt-oss-120b-medium",
}


def resolve_model(name: str) -> str:
    """Resolve model aliases case-insensitively or return original name."""
    clean = name.strip()
    return MODEL_ALIASES.get(clean.lower(), clean)


DEFAULTS = {
    "AGY_PATH": "/root/.local/bin/agy",
    "AGY_HOME": "/root",
    "AGY_WORKSPACE": "/root",
    "AGY_TIMEOUT_SECONDS": "900",
    "MAX_PROMPT_CHARS": "12000",
    "MAX_OUTPUT_BYTES": "1048576",
    "MAX_REPLY_CHARS": "30000",
    "AGY_SKIP_PERMISSIONS": "true",
    "AGY_WRITE_PATHS": "",
    "AGY_HOST_ACCESS": "restricted",
    "STATE_DIR": "/var/lib/agy-telegram-remote",
    "RESULT_RETENTION_DAYS": "7",
    "AGY_MODEL": "",
}
KEYS = frozenset(DEFAULTS) | {"TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS"}


class ConfigError(ValueError):
    """Messages must not contain configuration values or secrets."""


def read_private_text(path: Path, limit: int = 65536) -> str:
    """Reject symlinks, non-regular files and oversized inputs before reading."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise ConfigError("配置必须是独立的普通文件，不能是链接或设备。")
        if meta.st_size > limit:
            raise ConfigError("配置文件过大。")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ConfigError("配置文件过大。")
        return raw.decode("utf-8-sig")
    finally:
        os.close(fd)


def parse_env(text: str) -> dict[str, str]:
    """A limited dotenv subset; NEVER source/eval or expand $variables."""
    values: dict[str, str] = {}
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigError(f"配置第 {number} 行格式错误。")
        if key in values:
            raise ConfigError(f"配置项重复：{key}。")
        if value.startswith(("'", '"')):
            try:
                parts = shlex.split(value, comments=True, posix=True)
            except ValueError:
                raise ConfigError(f"配置第 {number} 行引号不完整。") from None
            if len(parts) != 1:
                raise ConfigError(f"配置第 {number} 行格式错误。")
            value = parts[0]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if any(c in value for c in ("\x00", "\r", "\n")):
            raise ConfigError(f"配置第 {number} 行包含控制字符。")
        values[key] = value
    return values


def integer(values: Mapping[str, str], key: str, minimum: int, maximum: int) -> int:
    try:
        result = int(values[key])
    except (KeyError, ValueError, TypeError):
        raise ConfigError(f"{key} 必须是整数。") from None
    if not minimum <= result <= maximum:
        raise ConfigError(f"{key} 超出允许范围 {minimum}..{maximum}。")
    return result


def absolute_path(value: str, key: str) -> Path:
    # Keeping deployment paths simple also prevents systemd specifier injection.
    if not PATH_RE.fullmatch(value) or ".." in Path(value).parts:
        raise ConfigError(f"{key} 必须是无空格、无特殊字符的绝对路径。")
    path = Path(value)
    if path == Path("/"):
        raise ConfigError(f"{key} 不能是根目录。")
    return path


def write_paths(value: str) -> tuple[Path, ...]:
    """Explicit directory grants, never a blanket writable root or unit syntax."""
    if not value:
        return ()
    parts = value.split(",")
    if len(parts) > 16 or any(not part for part in parts):
        raise ConfigError("AGY_WRITE_PATHS 必须为最多 16 个逗号分隔的绝对目录，或留空。")
    paths = tuple(dict.fromkeys(absolute_path(part, "AGY_WRITE_PATHS") for part in parts))
    # Require specific application directories rather than whole system trees.
    if any(len(path.parts) < 3 for path in paths):
        raise ConfigError("AGY_WRITE_PATHS 必须指定具体应用目录，不能开放整个顶层系统目录。")
    return paths


@dataclass(frozen=True)
class Settings:
    token: str
    allowed: frozenset[int]
    agy: Path
    home: Path
    workspace: Path
    timeout: int = 900
    max_prompt: int = 12000
    max_output: int = 1048576
    max_reply: int = 30000
    skip_permissions: bool = True
    state_dir: Path = Path("/var/lib/agy-telegram-remote")
    retention_days: int = 7
    model: str = ""
    owner_id: int = 0
    write_paths: tuple[Path, ...] = ()
    host_access: str = "restricted"

    def __post_init__(self) -> None:
        if self.owner_id == 0 and self.allowed:
            object.__setattr__(self, "owner_id", sorted(self.allowed)[0])

    @classmethod
    def from_mapping(cls, original: Mapping[str, str]) -> "Settings":
        if set(original) - KEYS:
            raise ConfigError("配置包含未知项目，请检查配置项拼写。")
        values = DEFAULTS | dict(original)
        token = values.get("TELEGRAM_BOT_TOKEN", "")
        if not TOKEN_RE.fullmatch(token):
            raise ConfigError("Telegram Bot Token 格式不正确。")
        ids = values.get("ALLOWED_USER_IDS", "").split(",")
        if not 1 <= len(ids) <= 32 or any(not re.fullmatch(r"[0-9]+", x.strip()) for x in ids):
            raise ConfigError("白名单必须包含 1..32 个以逗号分隔的数字用户 ID。")
        parsed_ids = [int(x.strip()) for x in ids]
        allowed = frozenset(parsed_ids)
        owner_id = parsed_ids[0]
        if any(not 0 < uid < 2**53 for uid in allowed):
            raise ConfigError("Telegram 数字 ID 超出范围。")
        permission = values["AGY_SKIP_PERMISSIONS"].strip().lower()
        host_access = values["AGY_HOST_ACCESS"].strip().lower()
        if host_access not in {"restricted", "full"}:
            raise ConfigError("AGY_HOST_ACCESS 必须是 restricted 或 full。")
        if permission not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ConfigError("AGY_SKIP_PERMISSIONS 必须是 true 或 false。")
        work = absolute_path(values["AGY_WORKSPACE"], "AGY_WORKSPACE")
        home = absolute_path(values.get("AGY_HOME", "/root"), "AGY_HOME")
        base = home if str(home) != "/" else Path("/root")
        if work != base and base not in work.parents:
            raise ConfigError(f"工作目录必须在 {base} 内。")
        state = absolute_path(values["STATE_DIR"], "STATE_DIR")
        state_base = Path("/var/lib/agy-telegram-remote")
        if state != state_base and state_base not in state.parents:
            raise ConfigError("STATE_DIR 必须在 /var/lib/agy-telegram-remote 内。")
        model = resolve_model(values.get("AGY_MODEL", ""))
        if model and not MODEL_RE.fullmatch(model):
            raise ConfigError("AGY_MODEL 格式不正确，仅支持字母、数字、点、下划线与连字符。")
        return cls(
            token=token, allowed=allowed,
            agy=absolute_path(values["AGY_PATH"], "AGY_PATH"),
            home=home,
            workspace=work,
            timeout=integer(values, "AGY_TIMEOUT_SECONDS", 10, 86400),
            max_prompt=integer(values, "MAX_PROMPT_CHARS", 1, 100000),
            max_output=integer(values, "MAX_OUTPUT_BYTES", 1024, 16777216),
            max_reply=integer(values, "MAX_REPLY_CHARS", 1000, 100000),
            skip_permissions=permission in {"true", "1", "yes", "on"},
            state_dir=state,
            retention_days=integer(values, "RESULT_RETENTION_DAYS", 1, 30),
            model=model,
            owner_id=owner_id,
            write_paths=write_paths(values["AGY_WRITE_PATHS"]),
            host_access=host_access,
        )

    @classmethod
    def load(cls, path: Path) -> "Settings":
        return cls.from_mapping(parse_env(read_private_text(path)))


def merged_config(old: Mapping[str, str], token: str, ids: str,
                  home: str = "/root", enable_auto: bool = False) -> dict[str, str]:
    """Fresh installs auto-approve; upgrades preserve old security choices."""
    values = DEFAULTS | {key: value for key, value in old.items() if key in KEYS}
    if old and "AGY_SKIP_PERMISSIONS" not in old:
        values["AGY_SKIP_PERMISSIONS"] = "false"
    if not old.get("AGY_PATH") or (home == "/root" and "/home/agy-tg" in old.get("AGY_PATH", "")):
        values["AGY_PATH"] = home + "/.local/bin/agy"
    values["AGY_HOME"] = home
    if not old.get("AGY_WORKSPACE") or (home == "/root" and not old.get("AGY_WORKSPACE", "").startswith("/root")):
        values["AGY_WORKSPACE"] = home
    if token:
        values["TELEGRAM_BOT_TOKEN"] = token
    if ids:
        values["ALLOWED_USER_IDS"] = ids
    if enable_auto:
        values["AGY_SKIP_PERMISSIONS"] = "true"
    Settings.from_mapping(values)
    return values


def serialize_env(values: Mapping[str, str]) -> str:
    Settings.from_mapping(values)
    # All accepted values are single-line; shlex.quote does not execute anything.
    order = ["TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS", *DEFAULTS]
    return "# Managed configuration; never commit this file.\n" + "".join(
        f"{key}={shlex.quote(values[key])}\n" for key in order
    )


def check_no_symlink(path: Path) -> None:
    """Check each existing component; installer also controls parent ownership."""
    for item in [*reversed(path.parents), path]:
        try:
            if item.is_symlink():
                raise ConfigError("部署路径包含符号链接，已停止。")
        except PermissionError:
            pass
