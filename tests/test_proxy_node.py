"""Share-link parsing only; no real credentials or network access."""
import base64
import json
import os
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from proxy_node import NodeError, build_config, parse_node

UUID = "00000000-0000-4000-8000-000000000000"


class ProxyNodeTests(unittest.TestCase):
    def test_vless_reality(self):
        result = parse_node(f"vless://{UUID}@example.test:443?security=reality&sni=example.test&fp=chrome&pbk=testkey&sid=01&flow=xtls-rprx-vision")
        self.assertEqual(result["type"], "vless")
        self.assertEqual(result["tls"]["reality"]["public_key"], "testkey")
        self.assertEqual(result["flow"], "xtls-rprx-vision")

    def test_vmess_websocket(self):
        data = {"v": "2", "add": "example.test", "port": "443", "id": UUID,
                "aid": "0", "net": "ws", "host": "example.test", "path": "/test", "tls": "tls"}
        link = base64.b64encode(json.dumps(data).encode()).decode()
        result = parse_node("vmess://" + link)
        self.assertEqual(result["transport"]["path"], "/test")
        self.assertTrue(result["tls"]["enabled"])

    def test_anytls_and_hysteria(self):
        self.assertEqual(parse_node("anytls://dummy@example.test:443?sni=example.test")["type"], "anytls")
        self.assertEqual(parse_node("hy2://dummy@example.test:443?sni=example.test")["type"], "hysteria2")

    def test_shadowsocks_standard_and_base64(self):
        userinfo = base64.urlsafe_b64encode(b"aes-128-gcm:dummy").decode().rstrip("=")
        result = parse_node(f"ss://{userinfo}@example.test:443#test")
        self.assertEqual(result["method"], "aes-128-gcm")
        self.assertEqual(result["password"], "dummy")
        encoded = base64.urlsafe_b64encode(b"aes-128-gcm:dummy@example.test:443").decode().rstrip("=")
        self.assertEqual(parse_node("ss://" + encoded), result)

    def test_http_inbound_is_loopback_only(self):
        config = build_config(parse_node("anytls://dummy@example.test:443"), 42000)
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(config["route"]["final"], "node")

    def test_reject_insecure_and_unknown(self):
        for url in ("anytls://dummy@example.test:443?insecure=1",
                    f"vless://{UUID}@example.test:443?type=xhttp",
                    f"vless://{UUID}@example.test:443?foo=bar",
                    f"vless://{UUID}@example.test:443?security=tls&security=none"):
            with self.subTest(url=url), self.assertRaises(NodeError):
                parse_node(url)

    def test_errors_do_not_contain_credentials(self):
        with self.assertRaises(NodeError) as context:
            parse_node("vless://private-secret@example.test:443")
        self.assertNotIn("private-secret", str(context.exception))

    @unittest.skipUnless(os.environ.get("AGY_TEST_SING_BOX"), "sing-box binary not supplied")
    def test_generated_configs_accepted_by_sing_box(self):
        vmess = {"v": "2", "add": "example.test", "port": "443", "id": UUID,
                 "aid": "0", "net": "ws", "host": "example.test", "path": "/test", "tls": "tls"}
        encoded = base64.b64encode(json.dumps(vmess).encode()).decode()
        public_key = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
        ss = base64.urlsafe_b64encode(b"aes-128-gcm:dummy").decode().rstrip("=")
        links = [f"vless://{UUID}@example.test:443?security=reality&sni=example.test&fp=chrome&pbk={public_key}",
                 "vmess://" + encoded, "anytls://dummy@example.test:443?sni=example.test",
                 "trojan://dummy@example.test:443?sni=example.test",
                 "hy2://dummy@example.test:443?sni=example.test", f"ss://{ss}@example.test:443"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.json"
            for link in links:
                with self.subTest(protocol=link.split(":", 1)[0]):
                    path.write_text(json.dumps(build_config(parse_node(link), 42000)), encoding="utf-8")
                    result = subprocess.run([os.environ["AGY_TEST_SING_BOX"], "check", "-c", str(path)],
                                            capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get("AGY_TEST_SING_BOX"), "sing-box binary not supplied")
    def test_local_vless_forwards_http_through_project_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "8")
                self.end_headers()
                self.wfile.write(b"proxy-ok")

            def log_message(self, *args):
                pass

        def unused_port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        server_port, proxy_port = unused_port(), unused_port()
        if server_port == proxy_port:
            self.skipTest("OS allocated the same free port twice")
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=http_server.serve_forever, daemon=True)
        worker.start()
        processes = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                server_config = {"inbounds": [{"type": "vless", "listen": "127.0.0.1",
                                               "listen_port": server_port, "users": [{"uuid": UUID}]}],
                                 "outbounds": [{"type": "direct", "tag": "direct"}],
                                 "route": {"final": "direct"}}
                client_config = build_config(parse_node(f"vless://{UUID}@127.0.0.1:{server_port}"), proxy_port)
                for index, config in enumerate((server_config, client_config)):
                    path = Path(directory) / f"proxy-{index}.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    processes.append(subprocess.Popen([os.environ["AGY_TEST_SING_BOX"], "run", "-c", str(path)],
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                deadline = time.monotonic() + 10
                for listen_port in (server_port, proxy_port):
                    while True:
                        if any(proc.poll() is not None for proc in processes):
                            self.fail("Local proxy process exited")
                        try:
                            with socket.create_connection(("127.0.0.1", listen_port), timeout=0.2):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                self.fail("Local proxy did not start")
                            time.sleep(0.05)
                connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
                try:
                    connection.request("GET", f"http://127.0.0.1:{http_server.server_port}/test")
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.read()), (200, b"proxy-ok"))
                finally:
                    connection.close()
        finally:
            for proc in processes:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            http_server.shutdown()
            http_server.server_close()
            worker.join(timeout=5)
