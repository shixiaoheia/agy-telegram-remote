"""Small Telegram Bot API client; no token-bearing URLs are logged."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
from http.client import HTTPException
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code: int = 0, retry_after: int = 0):
        super().__init__(f"Telegram API error ({code or 'network/protocol'})")
        self.code = code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str, *, base_url: str = "https://api.telegram.org",
                 request_timeout: float = 25.0):
        # base_url injection is only used by offline tests, never configuration.
        root = base_url.rstrip("/")
        self._endpoint = f"{root}/bot{token}/"
        self._file_endpoint = f"{root}/file/bot{token}/"
        self.request_timeout = request_timeout

    def _request(self, method: str, payload: dict, request_timeout: float | None = None) -> object:
        request = Request(
            self._endpoint + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        http_code = 0
        try:
            try:
                response = urlopen(request, timeout=request_timeout or self.request_timeout)
            except HTTPError as error:
                http_code = error.code
                response = error
            with response:
                raw = response.read(2097153)
            if len(raw) > 2097152:
                raise TelegramError(http_code)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TelegramError(http_code)
            if value.get("ok") is not True:
                code = value.get("error_code", http_code)
                parameters = value.get("parameters")
                retry = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
                raise TelegramError(
                    code if type(code) is int else 0,
                    min(retry, 86400) if type(retry) is int and retry > 0 else 0,
                )
            if http_code or "result" not in value:
                raise TelegramError(http_code)
            return value["result"]
        except TelegramError:
            raise
        except (URLError, OSError, socket.timeout, ValueError, HTTPException, RecursionError):
            # The original exception can include the token-bearing request URL.
            raise TelegramError() from None

    async def call(self, method: str, **payload) -> object:
        return await asyncio.to_thread(self._request, method, payload)

    async def get_updates(self, *, offset: int, limit: int, timeout: int,
                          allowed_updates: list[str]) -> object:
        """Long-poll with a bounded transport timeout.

        A healthy long poll returns as soon as an update arrives. A damaged TCP
        connection must not consume the generic 25-second API timeout and hold
        the next Telegram command hostage.
        """
        payload = {
            "offset": offset, "limit": limit, "timeout": timeout,
            "allowed_updates": allowed_updates,
        }
        request_timeout = max(12.0, float(timeout) + 3.0)
        return await asyncio.to_thread(self._request, "getUpdates", payload, request_timeout)

    def _download_file(self, file_path: str, destination: Path, maximum: int) -> int:
        """Download a Telegram file into a new private regular file, bounded in bytes."""
        if (not isinstance(file_path, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", file_path)
                or file_path.startswith("/") or ".." in file_path.split("/")):
            raise TelegramError()
        if maximum < 1 or destination.parent.is_symlink() or destination.exists():
            raise TelegramError()
        try:
            request = Request(self._file_endpoint + file_path, method="GET")
            with urlopen(request, timeout=self.request_timeout) as response:
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > maximum):
                    raise TelegramError(413)
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    total = 0
                    with os.fdopen(fd, "wb") as output:
                        while True:
                            chunk = response.read(min(65536, maximum + 1 - total))
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > maximum:
                                raise TelegramError(413)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                except BaseException:
                    try:
                        destination.unlink(missing_ok=True)
                    finally:
                        raise
            return total
        except TelegramError:
            raise
        except (URLError, OSError, socket.timeout, ValueError, HTTPException, RecursionError):
            raise TelegramError() from None

    async def download_file(self, file_path: str, destination: Path, maximum: int) -> int:
        return await asyncio.to_thread(self._download_file, file_path, destination, maximum)

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None,
                   parse_mode: str | None = None) -> object:
        # Retry ONLY a definite 429 rejection, not an ambiguous network failure.
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        for attempt in range(3):
            try:
                return await self.call("sendMessage", **payload)
            except TelegramError as error:
                if error.code != 429 or not 0 < error.retry_after <= 10 or attempt == 2:
                    raise
                await asyncio.sleep(error.retry_after)

    async def edit(self, chat_id: int, message_id: int, text: str,
                   reply_markup: dict | None = None) -> object:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("editMessageText", **payload)

    async def set_commands(self, commands: list[dict[str, str]]) -> object:
        return await self.call("setMyCommands", commands=commands)

    async def answer_callback_query(self, callback_query_id: str, text: str = "",
                                    show_alert: bool = False) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_query_id,
                            text=text, show_alert=show_alert)
        except TelegramError:
            pass


def chunks_utf16(text: str, units: int = 3500) -> list[str]:
    """Never split a Python code point; account for Telegram's UTF-16 lengths."""
    if units < 2:
        raise ValueError("Chunk limit is too small")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        # Replace invalid lone surrogates rather than failing JSON UTF-8 encoding.
        if 0xD800 <= ord(char) <= 0xDFFF:
            char = "\ufffd"
        length = 2 if ord(char) > 0xFFFF else 1
        if size + length > units:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += length
    if current:
        chunks.append("".join(current))
    return chunks
