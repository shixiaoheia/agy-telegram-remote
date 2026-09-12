import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from .common import settings_at
except ImportError:
    from common import settings_at
from agy_runner import (Runner, build_command, child_environment, classify,
                        live_group, parse_result, read_bounded)

class ParserTests(unittest.TestCase):
    def parse(self, value, err=b"", code=0):
        return parse_result(json.dumps(value).encode(), err, code)

    def test_success(self):
        self.assertEqual(self.parse({"status": "SUCCESS", "response": " hello "}).text, "hello")

    def test_empty_success_not_completed(self):
        self.assertEqual(self.parse({"status": "SUCCESS", "response": " \n"}).outcome, "no_text")

    def test_missing_or_invalid_terminal_status(self):
        for value in (
            {"response": "text"},
            {"event": "init"},
            {"status": "RUNNING", "response": "thinking"},
            {"status": "WAITING", "response": "waiting"},
            {"status": None, "response": "text"},
        ):
            with self.subTest(value=value):
                self.assertNotEqual(self.parse(value).outcome, "success")

    def test_invalid_response_types(self):
        for value in (None, [], {}, 42):
            self.assertEqual(self.parse({"status": "SUCCESS", "response": value}).outcome, "invalid")

    def test_errors_never_success(self):
        self.assertEqual(self.parse({"status": "ERROR", "response": "some text"}).outcome, "error")
        self.assertEqual(self.parse({"status": "SUCCESS", "response": "text"}, code=1).outcome, "error")
        self.assertEqual(self.parse({"status": "SUCCESS", "response": "text", "error": "failed"}).outcome, "error")

    def test_corrupt_or_streaming_json_never_plaintext(self):
        values = [
            b"", b"hello", b'{"status":"SUCCESS"',
            b'{"status":"SUCCESS","response":"ok"}\n{"status":"ERROR"}',
            b'warning\n{"status":"SUCCESS","response":"ok"}',
            b'[{"status":"SUCCESS","response":"ok"}]',
            b'{"status":"SUCCESS","response":"ok","status":"ERROR"}',
            b'{"status":"SUCCESS","response":"ok","usage":NaN}',
            b'\xff{"status":"SUCCESS","response":"ok"}',
        ]
        for value in values:
            with self.subTest(value=value):
                result = parse_result(value, b"", 0)
                self.assertEqual(result.outcome, "invalid")
                self.assertEqual(result.text, "")

    def test_diagnostic_categories(self):
        self.assertEqual(classify("authentication required"), "auth")
        self.assertEqual(classify("quota exhausted"), "quota")
        self.assertEqual(classify("DNS resolution failed"), "network")
        self.assertEqual(classify("unrelated error"), "unknown")

    def test_permission_not_hidden(self):
        value = self.parse({"status": "SUCCESS", "response": "I tried"}, b"tool soft-denied")
        self.assertEqual(value.outcome, "permission")

    def test_invalid_unicode_response_rejected(self):
        result = parse_result(b'{"status":"SUCCESS","response":"\\ud800"}', b"", 0)
        self.assertEqual(result.outcome, "invalid")

    def test_random_garbage_never_raises_or_succeeds(self):
        import random
        rng = random.Random(20260912)
        for _ in range(200):
            data = rng.randbytes(rng.randrange(0, 500))
            self.assertNotEqual(parse_result(data, b"", 0).outcome, "success")

    def test_sensitive_environment_is_not_inherited(self):
        env = child_environment(Path("/home/test"), {
            "TELEGRAM_BOT_TOKEN": "secret", "PYTHONPATH": "malicious",
            "AWS_SECRET_ACCESS_KEY": "secret", "SSH_AUTH_SOCK": "/tmp/agent",
            "HTTPS_PROXY": "http://localhost:9000",
        })
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertIn("HTTPS_PROXY", env)

class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_bounded_and_overflow_marked(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 4096)
        stream.feed_eof()
        overflow = asyncio.Event()
        output = await read_bounded(stream, 1024, overflow)
        self.assertEqual(len(output.data), 1024)
        self.assertEqual(output.total, 4096)
        self.assertTrue(overflow.is_set())

@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group integration")
class RealProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.settings = settings_at(self.directory)
        self.children = []
        self.pidfile = self.directory / "child.pid"

    async def asyncTearDown(self):
        if self.pidfile.exists():
            try:
                pid = int(self.pidfile.read_text())
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        self.temp.cleanup()

    def executable(self, body):
        self.settings.agy.write_text("#!" + sys.executable + "\n" + body)
        self.settings.agy.chmod(0o700)

    def runner(self, **kwargs):
        return Runner(self.settings, outer_grace=0, terminate_grace=0.1, **kwargs)

    async def test_real_success_and_arguments(self):
        self.executable(
            "import json,sys,os\n"
            "assert sys.argv[sys.argv.index('--output-format')+1] == 'json'\n"
            "assert '--dangerously-skip-permissions' in sys.argv\n"
            f"assert os.getcwd() == {str(self.settings.workspace)!r}\n"
            "assert 'TELEGRAM_BOT_TOKEN' not in os.environ\n"
            "print(json.dumps({'status':'SUCCESS','response':'ok'}))\n"
        )
        result = await self.runner().run("hello", asyncio.Event())
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.text, "ok")

    async def test_real_success_with_model(self):
        self.executable(
            "import json,sys\n"
            "assert sys.argv[sys.argv.index('--model')+1] == 'gemini-3.1-pro-high'\n"
            "print(json.dumps({'status':'SUCCESS','response':'ok-model'}))\n"
        )
        result = await self.runner().run("hello", asyncio.Event(), model="gemini-3.1-pro-high")
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.text, "ok-model")
        self.assertEqual(result.model, "gemini-3.1-pro-high")

    def test_build_command_model_option(self):
        cmd_default = build_command(self.settings, "prompt")
        self.assertNotIn("--model", cmd_default)
        cmd_model = build_command(self.settings, "prompt", model="claude-sonnet-4-6")
        self.assertIn("--model", cmd_model)
        idx = cmd_model.index("--model")
        self.assertEqual(cmd_model[idx + 1], "claude-sonnet-4-6")

    def test_build_command_effort_and_mode_options(self):
        cmd = build_command(self.settings, "prompt", effort="high", mode="plan")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertIn("--mode", cmd)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")

    async def test_no_shell_interpolation(self):
        marker = self.directory / "must-not-exist"
        self.executable("import json,sys\nprint(json.dumps({'status':'SUCCESS','response':sys.argv[-1]}))\n")
        prompt = f"$(touch {marker}); echo danger"
        result = await self.runner().run(prompt, asyncio.Event())
        self.assertEqual(result.text, prompt)
        self.assertFalse(marker.exists())

    async def test_cancel_before_start(self):
        self.executable("raise RuntimeError('must not execute')\n")
        cancel = asyncio.Event()
        cancel.set()
        result = await self.runner().run("x", cancel)
        self.assertEqual(result.outcome, "cancelled")
        self.assertIsNone(result.exit_code)

    async def test_cancel_while_running(self):
        self.executable("import time\nwhile True: time.sleep(1)\n")
        cancel = asyncio.Event()
        task = asyncio.create_task(self.runner().run("x", cancel))
        await asyncio.sleep(0.1)
        cancel.set()
        result = await asyncio.wait_for(task, 4)
        self.assertEqual(result.outcome, "cancelled")
        self.assertTrue(result.cleanup_ok)

    async def test_external_task_cancellation_cleans_process(self):
        self.executable("import time\nwhile True: time.sleep(1)\n")
        task = asyncio.create_task(self.runner().run("x", asyncio.Event()))
        await asyncio.sleep(0.1)
        task.cancel()
        result = await asyncio.wait_for(task, 4)
        self.assertEqual(result.outcome, "cancelled")
        self.assertTrue(result.cleanup_ok)

    async def test_timeout(self):
        from dataclasses import replace
        self.settings = replace(self.settings, timeout=0.15)
        self.executable("import time\ntime.sleep(20)\n")
        result = await asyncio.wait_for(self.runner().run("x", asyncio.Event()), 4)
        self.assertEqual(result.outcome, "timed_out")
        self.assertTrue(result.cleanup_ok)

    async def test_output_overflow_is_not_parsed(self):
        from dataclasses import replace
        self.settings = replace(self.settings, max_output=1024)
        self.executable("import sys,time\nsys.stdout.write('x'*1000000);sys.stdout.flush();time.sleep(20)\n")
        result = await asyncio.wait_for(self.runner().run("x", asyncio.Event()), 4)
        self.assertEqual(result.outcome, "output_limit")
        self.assertEqual(result.text, "")
        self.assertGreater(result.stdout_bytes, 1024)

    async def test_nonzero_exit_not_success(self):
        self.executable("import json,sys\nprint(json.dumps({'status':'SUCCESS','response':'ok'}));sys.exit(2)\n")
        result = await self.runner().run("x", asyncio.Event())
        self.assertEqual(result.outcome, "error")

    async def test_parent_exits_child_keeps_pipes(self):
        await self.check_orphan(close_pipes=False, ignore_term=False)

    async def test_parent_exits_child_closes_pipes(self):
        await self.check_orphan(close_pipes=True, ignore_term=False)

    async def test_sigkill_for_child_ignoring_sigterm(self):
        await self.check_orphan(close_pipes=False, ignore_term=True)

    async def check_orphan(self, close_pipes, ignore_term):
        self.executable(
            "import os,signal,time,json\n"
            f"pidfile={str(self.pidfile)!r}\n"
            "pid=os.fork()\n"
            "if pid==0:\n"
            f"    if {ignore_term!r}: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"    if {close_pipes!r}: os.close(1); os.close(2)\n"
            "    with open(pidfile,'w') as f: f.write(str(os.getpid()))\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "while not os.path.exists(pidfile): time.sleep(0.005)\n"
            "print(json.dumps({'status':'SUCCESS','response':'ok'}),flush=True)\n"
            "os._exit(0)\n"
        )
        result = await asyncio.wait_for(self.runner().run("x", asyncio.Event()), 4)
        self.assertEqual(result.outcome, "success")
        self.assertTrue(result.cleanup_ok)
        pid = int(self.pidfile.read_text())
        try:
            status = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
            self.assertIn(status, {"Z", "X"})
        except FileNotFoundError:
            pass

    async def test_detached_child_holding_pipe_blocks_new_jobs(self):
        self.executable(
            "import os,time,json\n"
            f"pidfile={str(self.pidfile)!r}\n"
            "pid=os.fork()\n"
            "if pid==0:\n"
            "    os.setsid()\n"
            "    with open(pidfile,'w') as f: f.write(str(os.getpid()))\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "while not os.path.exists(pidfile): time.sleep(0.005)\n"
            "print(json.dumps({'status':'SUCCESS','response':'ok'}),flush=True)\n"
            "os._exit(0)\n"
        )
        runner = self.runner()
        result = await asyncio.wait_for(runner.run("x", asyncio.Event()), 5)
        self.assertEqual(result.outcome, "cleanup_failed")
        self.assertTrue(runner.blocked)
        # The fixture's asyncTearDown explicitly kills this escaped test child.

    async def test_cancellation_during_spawn_does_not_lose_child(self):
        from unittest.mock import patch
        self.executable("import time\ntime.sleep(30)\n")
        actual_spawn = asyncio.create_subprocess_exec
        created, release = asyncio.Event(), asyncio.Event()
        holder = {}
        async def delayed_spawn(*args, **kwargs):
            process = await actual_spawn(*args, **kwargs)
            holder["process"] = process
            created.set()
            await release.wait()
            return process
        with patch("agy_runner.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            task = asyncio.create_task(self.runner().run("x", asyncio.Event()))
            await created.wait()
            task.cancel()
            release.set()
            result = await asyncio.wait_for(task, 5)
        self.assertEqual(result.outcome, "cancelled")
        self.assertTrue(result.cleanup_ok)
        self.assertIsNotNone(holder["process"].returncode)

    async def test_missing_executable(self):
        result = await self.runner().run("x", asyncio.Event())
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.category, "process")

    async def test_blocked_runner_does_not_start(self):
        runner = self.runner()
        runner.blocked = True
        result = await runner.run("x", asyncio.Event())
        self.assertEqual(result.outcome, "cleanup_failed")
        self.assertFalse(result.cleanup_ok)
