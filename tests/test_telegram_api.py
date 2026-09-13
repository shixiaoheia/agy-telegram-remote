import asyncio
import json
import threading
import tempfile
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch
try:
    from .common import DUMMY_TOKEN
except ImportError:
    from common import DUMMY_TOKEN
from telegram_api import TelegramAPI, TelegramError, chunks_utf16

class TelegramHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.requests = []
        self.file_body = b"fixture"
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((self.path.rsplit("/", 1)[-1], json.loads(body)))
                status, value = outer.responses.pop(0)
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                raw = outer.file_body
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = TelegramAPI(
            DUMMY_TOKEN, base_url=f"http://127.0.0.1:{self.server.server_port}",
            request_timeout=2,
        )

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=2)

    async def test_real_http_send_payload(self):
        self.responses.append((200, {"ok": True, "result": {"message_id": 1}}))
        await self.api.send(12345, "hello")
        method, payload = self.requests[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], 12345)
        self.assertEqual(payload["text"], "hello")
        self.assertNotIn("parse_mode", payload)

    async def test_explicit_429_retries_only_delivery(self):
        self.responses.extend([
            (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}),
            (200, {"ok": True, "result": {"message_id": 1}}),
        ])
        with patch("telegram_api.asyncio.sleep", new_callable=AsyncMock):
            await self.api.send(12345, "hello")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(method == "sendMessage" for method, _ in self.requests))

    async def test_ambiguous_failure_not_retried(self):
        self.responses.append((502, b"bad gateway"))
        with self.assertRaises(TelegramError) as context:
            await self.api.send(12345, "hello")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(DUMMY_TOKEN, str(context.exception))

    async def test_fatal_telegram_error_is_sanitized(self):
        self.responses.append((401, {"ok": False, "error_code": 401,
                                     "description": "credential " + DUMMY_TOKEN}))
        with self.assertRaises(TelegramError) as context:
            await self.api.call("getMe")
        self.assertEqual(context.exception.code, 401)
        self.assertNotIn(DUMMY_TOKEN, str(context.exception))

    async def test_rate_limit_retries_are_bounded(self):
        self.responses.extend([
            (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})
            for _ in range(3)
        ])
        with patch("telegram_api.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(TelegramError):
                await self.api.send(12345, "hello")
        self.assertEqual(len(self.requests), 3)

    async def test_excessive_retry_after_is_not_slept(self):
        self.responses.append((429, {"ok": False, "error_code": 429,
                                     "parameters": {"retry_after": 10000}}))
        with patch("telegram_api.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(TelegramError):
                await self.api.send(12345, "hello")
            sleep.assert_not_called()

    async def test_dispatch_process_store_http_failure_then_last(self):
        import tempfile
        import sys
        from pathlib import Path
        try:
            from .common import settings_at
        except ImportError:
            from common import settings_at
        from agy_runner import Runner
        from bot import Bridge
        from state_store import Store

        with tempfile.TemporaryDirectory() as temp:
            config = settings_at(Path(temp))
            marker = Path(temp) / "runs"
            config.agy.write_text(
                "#!" + sys.executable + "\n"
                "import json\n"
                f"with open({str(marker)!r}, 'a') as f: f.write('run\\n')\n"
                "print(json.dumps({'status':'SUCCESS','response':'offline pipeline OK'}))\n"
            )
            config.agy.chmod(0o700)
            store = Store(config.state_dir, config.allowed, config.max_reply, 7, config.token)
            bridge = Bridge(config, self.api, store, Runner(config, terminate_grace=0.1))
            self.responses.extend([
                (200, {"ok": True, "result": {"id": 10, "is_bot": True}}),
                (200, {"ok": True, "result": True}),
                (200, {"ok": True, "result": {"url": ""}}),
                (200, {"ok": True, "result": []}),
                (200, {"ok": True, "result": {"message_id": 1}}),
                # The completed task first removes the progress card's cancel button.
                (200, {"ok": True, "result": True}),
                # The result delivery then fails, while /last remains available.
                (502, b"temporary gateway error"),
                (200, {"ok": True, "result": {"message_id": 2}}),
            ])
            await bridge.initialize()
            def event(text, uid):
                return {"update_id": uid, "message": {
                    "from": {"id": 12345}, "chat": {"id": 12345, "type": "private"},
                    "text": text,
                }}
            await bridge.consume_updates([event("one harmless task", 1)])
            worker = bridge.slot.worker
            await worker
            self.assertEqual(store.load(12345)["delivery"], "failed_or_partial")
            await bridge.consume_updates([event("/last", 2)])
            await bridge.reply_worker
            self.assertEqual(marker.read_text(), "run\n")
            self.assertIn("offline pipeline OK", self.requests[-1][1]["text"])
            self.assertEqual(store.offset(), 3)
            store.close()

    async def test_incomplete_http_response_sanitized(self):
        from http.client import IncompleteRead
        with patch("telegram_api.urlopen", side_effect=IncompleteRead(b"partial", 50)):
            with self.assertRaises(TelegramError):
                await self.api.call("getMe")

    async def test_excessive_retry_integer_is_bounded(self):
        self.responses.append((429, {"ok": False, "error_code": 429,
                                     "parameters": {"retry_after": 10**100}}))
        with self.assertRaises(TelegramError) as context:
            await self.api.call("getUpdates")
        self.assertEqual(context.exception.retry_after, 86400)

    async def test_get_updates_payload(self):
        self.responses.append((200, {"ok": True, "result": []}))
        result = await self.api.call("getUpdates", offset=100, timeout=10,
                                     allowed_updates=["message"])
        self.assertEqual(result, [])
        self.assertEqual(self.requests[0][1]["offset"], 100)

    async def test_download_file_is_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "input.txt"
            size = await self.api.download_file("documents/input.txt", target, 10)
            self.assertEqual(size, len(self.file_body))
            self.assertEqual(target.read_bytes(), self.file_body)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(TelegramError):
                await self.api.download_file("../secret", Path(temp) / "bad", 10)

class ChunkTests(unittest.TestCase):
    def test_emoji_utf16_limit(self):
        text = ("a😀中" * 3000)
        chunks = chunks_utf16(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))

    def test_lone_surrogate_replaced(self):
        self.assertEqual(chunks_utf16("a\ud800b"), ["a\ufffdb"])

    def test_empty_text(self):
        self.assertEqual(chunks_utf16(""), [])
