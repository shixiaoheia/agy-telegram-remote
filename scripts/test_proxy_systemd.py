#!/usr/bin/env python3
"""Exercise actual systemd switching in an explicitly disposable CI VM only."""
from __future__ import annotations

import contextlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import proxy_manager as pm
from proxy_node import build_config, parse_node

if (os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("CI") != "true" or os.environ.get("AGY_TEST_ISOLATED_SYSTEMD") != "1"):
    raise SystemExit("This test requires an explicitly isolated GitHub Actions VM and root.")
if Path("/proc/1/comm").read_text().strip() != "systemd":
    raise SystemExit("This test requires systemd as PID 1.")
binary = Path(os.environ["AGY_TEST_SING_BOX"])
if not binary.is_file():
    raise SystemExit("Missing verified test binary.")

suffix = str(os.getpid())
prefix = "agy-proxy-ci-" + suffix
bot_name = prefix + "-bot.service"
proxy_name = prefix + "-proxy.service"
bot_unit = Path("/etc/systemd/system") / bot_name
proxy_unit = Path("/etc/systemd/system") / proxy_name
drop_dir = Path("/etc/systemd/system") / (bot_name + ".d")
if any(p.exists() or p.is_symlink() for p in (bot_unit, proxy_unit, drop_dir)):
    raise SystemExit("Refusing to overwrite existing test service paths.")


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "8")
        self.end_headers()
        self.wfile.write(b"proxy-ok")

    def log_message(self, *args):
        pass


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, timeout=30, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
proc = None
config_dir = Path(tempfile.mkdtemp(prefix=prefix + "-", dir="/etc"))
engine_dir = Path(tempfile.mkdtemp(prefix=prefix + "-", dir="/opt"))
engine_dir.chmod(0o755)
try:
    engine = engine_dir / "sing-box"
    shutil.copyfile(binary, engine)
    engine.chmod(0o755)
    pm.CONFIG_DIR = config_dir
    pm.CONFIG = config_dir / "proxy.json"
    pm.ENV = config_dir / "proxy.env"
    pm.JOURNAL = config_dir / ".proxy-transaction.json"
    pm.ENGINE = engine
    pm.UNIT = proxy_unit
    pm.DROP_DIR = drop_dir
    pm.DROP = drop_dir / "90-agy-managed-proxy.conf"
    pm.SERVICE = bot_name
    pm.PROXY_SERVICE = proxy_name
    pm.ready = lambda: True  # The test bot is a dummy process, not the real application.

    def connectivity(local_port: int) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", local_port, timeout=4)
        try:
            connection.request("GET", f"http://127.0.0.1:{server.server_port}/test")
            response = connection.getresponse()
            if response.status != 200 or response.read() != b"proxy-ok":
                raise pm.ProxyError("Local test proxy did not forward correctly.")
        except OSError as exc:
            raise pm.ProxyError("Local test proxy did not forward correctly.") from exc
        finally:
            connection.close()

    pm.check_connectivity = connectivity
    uuid = "00000000-0000-4000-8000-000000000000"
    upstream_port, local_port, bad_port, bad_upstream_port = port(), port(), port(), port()
    if len({upstream_port, local_port, bad_port, bad_upstream_port}) != 4:
        raise RuntimeError("Ephemeral port collision")
    upstream = {"inbounds": [{"type": "vless", "listen": "127.0.0.1", "listen_port": upstream_port,
                               "users": [{"uuid": uuid}]}],
                "outbounds": [{"type": "direct", "tag": "direct"}], "route": {"final": "direct"}}
    upstream_config = config_dir / "upstream.json"
    upstream_config.write_text(json.dumps(upstream), encoding="utf-8")
    proc = subprocess.Popen([str(engine), "run", "-c", str(upstream_config)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while True:
        if proc.poll() is not None:
            raise RuntimeError("Test upstream exited")
        try:
            with socket.create_connection(("127.0.0.1", upstream_port), timeout=0.2):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise RuntimeError("Test upstream did not start")
            time.sleep(0.05)
    bot_unit.write_text("[Service]\nExecStart=/usr/bin/sleep 300\n", encoding="utf-8")
    command("systemctl", "daemon-reload")
    command("systemctl", "start", bot_name)
    good = build_config(parse_node(f"vless://{uuid}@127.0.0.1:{upstream_port}"), local_port)
    pm.apply(good, local_port)
    connectivity(local_port)
    pid = subprocess.check_output(["systemctl", "show", bot_name, "-p", "MainPID", "--value"], text=True).strip()
    environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    assert f"HTTPS_PROXY=http://127.0.0.1:{local_port}".encode() in environment
    old_config = pm.CONFIG.read_bytes()
    bad = build_config(parse_node(f"vless://{uuid}@127.0.0.1:{bad_upstream_port}"), bad_port)
    try:
        pm.apply(bad, bad_port)
    except (pm.ProxyError, OSError):
        pass
    else:
        raise AssertionError("Unreachable replacement was accepted")
    assert pm.CONFIG.read_bytes() == old_config
    assert not pm.JOURNAL.exists()
    assert pm.active(bot_name) and pm.active(proxy_name)
    pm.wait_listener(local_port)
    connectivity(local_port)
    print("SYSTEMD_PROXY_SWITCH_AND_ROLLBACK_OK")
except BaseException:
    # The isolated fixture contains only invented node credentials.
    subprocess.run(["journalctl", "-u", proxy_name, "-n", "40", "--no-pager"], check=False)
    raise
finally:
    command("systemctl", "stop", bot_name, check=False)
    command("systemctl", "disable", "--now", proxy_name, check=False)
    with contextlib.suppress(FileNotFoundError):
        bot_unit.unlink()
    with contextlib.suppress(FileNotFoundError):
        proxy_unit.unlink()
    shutil.rmtree(drop_dir, ignore_errors=True)
    command("systemctl", "daemon-reload", check=False)
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    shutil.rmtree(config_dir, ignore_errors=True)
    shutil.rmtree(engine_dir, ignore_errors=True)
