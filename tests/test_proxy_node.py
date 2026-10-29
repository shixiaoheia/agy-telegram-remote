"""Share-link parsing only; no real credentials or network access."""
import base64
import json
import os
import subprocess
import sys
import tempfile
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
