import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "project-handoff"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import app_server_client
from app_server_client import AppServerClient, LaunchResult


class RecordingInput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.was_closed = False

    def close(self):
        self.was_closed = True


class TrackedStringIO(io.StringIO):
    def __init__(self, value=""):
        super().__init__(value)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class DelayedEOF:
    def __init__(self, first_line, delay=0.1):
        self._first_line = first_line
        self._delay = delay
        self._unblock = threading.Event()
        self.was_closed = False

    def readline(self):
        if self._first_line is not None:
            line = self._first_line
            self._first_line = None
            return line
        self._unblock.wait(self._delay)
        return ""

    def close(self):
        self.was_closed = True
        self._unblock.set()


class FakeProcess:
    def __init__(self, responses=(), stdout=None, stderr=""):
        self.stdin = RecordingInput()
        self.stdout = stdout or TrackedStringIO(
            "".join(json.dumps(response) + "\n" for response in responses)
        )
        self.stderr = TrackedStringIO(stderr)
        self.terminated = False
        self.waited = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


class AppServerClientTests(unittest.TestCase):
    def make_client(self, process, run_command=None, request_timeout=1.0):
        if run_command is None:
            run_command = lambda command, **kwargs: None
        return AppServerClient(
            run_command=run_command,
            popen_factory=lambda command, **kwargs: process,
            request_timeout=request_timeout,
        )

    def assert_proxy_cleaned_up(self, process):
        self.assertTrue(process.stdin.was_closed)
        self.assertTrue(process.stdout.was_closed)
        self.assertTrue(process.stderr.was_closed)
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    def test_launch_starts_thread_and_turn(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {"id": 3, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        daemon_commands = []
        proxy_commands = []

        def run_command(command, **kwargs):
            daemon_commands.append(command)

        def popen_factory(command, **kwargs):
            proxy_commands.append(command)
            return process

        client = AppServerClient(
            run_command=run_command,
            popen_factory=popen_factory,
            request_timeout=1.0,
        )

        result = client.launch(
            cwd="/workspace/repo",
            prompt="resume from the handoff",
        )

        self.assertEqual(
            result,
            LaunchResult(thread_id="thr-new", turn_id="turn-new"),
        )
        self.assertEqual(
            [json.loads(line) for line in process.stdin.getvalue().splitlines()],
            [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "project-handoff",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                },
                {"method": "initialized", "params": {}},
                {
                    "id": 2,
                    "method": "thread/start",
                    "params": {"cwd": "/workspace/repo"},
                },
                {
                    "id": 3,
                    "method": "turn/start",
                    "params": {
                        "threadId": "thr-new",
                        "input": [
                            {
                                "type": "text",
                                "text": "resume from the handoff",
                            }
                        ],
                    },
                },
            ],
        )
        self.assertEqual(
            daemon_commands,
            [["codex", "app-server", "daemon", "start"]],
        )
        self.assertEqual(
            proxy_commands,
            [["codex", "app-server", "proxy"]],
        )
        self.assert_proxy_cleaned_up(process)

    def test_launch_surfaces_daemon_start_failure(self):
        process = FakeProcess()
        proxy_started = []

        def fail_daemon(command, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=7,
                cmd=command,
                stderr="Bearer should-not-leak",
            )

        client = AppServerClient(
            run_command=fail_daemon,
            popen_factory=lambda command, **kwargs: proxy_started.append(command),
            request_timeout=1.0,
        )

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "daemon.*7",
        ) as raised:
            client.launch("/workspace/repo", "private recovery prompt")

        self.assertNotIn("Bearer", str(raised.exception))
        self.assertNotIn("private recovery prompt", str(raised.exception))
        self.assertEqual(proxy_started, [])
        self.assertFalse(process.terminated)

    def test_launch_rejects_json_rpc_error(self):
        process = FakeProcess(
            [
                {
                    "id": 1,
                    "error": {
                        "code": -32000,
                        "message": "Bearer should-not-leak",
                    },
                }
            ]
        )
        client = self.make_client(process)

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "initialize.*-32000",
        ) as raised:
            client.launch("/workspace/repo", "private recovery prompt")

        self.assertNotIn("Bearer", str(raised.exception))
        self.assertNotIn("private recovery prompt", str(raised.exception))
        self.assert_proxy_cleaned_up(process)

    def test_launch_rejects_missing_thread_id(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {}}},
            ]
        )
        client = self.make_client(process)

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "thread/start.*thread id",
        ):
            client.launch("/workspace/repo", "resume from the handoff")

        self.assert_proxy_cleaned_up(process)

    def test_launch_rejects_missing_turn_id(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {"id": 3, "result": {"turn": {}}},
            ]
        )
        client = self.make_client(process)

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "turn/start.*turn id",
        ):
            client.launch("/workspace/repo", "resume from the handoff")

        self.assert_proxy_cleaned_up(process)

    def test_launch_times_out_waiting_for_response(self):
        initialize_response = json.dumps(
            {"id": 1, "result": {"capabilities": {}}}
        ) + "\n"
        process = FakeProcess(
            stdout=DelayedEOF(initialize_response),
        )
        client = self.make_client(
            process,
            request_timeout=0.01,
        )
        started_at = time.monotonic()

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "thread/start.*timed out",
        ):
            client.launch("/workspace/repo", "resume from the handoff")

        self.assertLess(time.monotonic() - started_at, 0.08)
        self.assert_proxy_cleaned_up(process)

    def test_notifications_do_not_consume_response_ids(self):
        process = FakeProcess(
            [
                {"method": "server/ready", "params": {}},
                {"id": 1, "result": {"capabilities": {}}},
                {
                    "method": "thread/started",
                    "params": {"thread": {"id": "thr-new"}},
                },
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {
                    "method": "turn/started",
                    "params": {"turn": {"id": "turn-new"}},
                },
                {"id": 3, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        client = self.make_client(process)

        result = client.launch(
            "/workspace/repo",
            "resume from the handoff",
        )

        self.assertEqual(
            result,
            LaunchResult(thread_id="thr-new", turn_id="turn-new"),
        )

    def test_launch_bounds_proxy_stderr_on_malformed_response(self):
        stderr = "x" * 100_000 + "Bearer should-not-leak"
        process = FakeProcess(
            stdout=TrackedStringIO("not-json\n"),
            stderr=stderr,
        )
        client = self.make_client(process)

        with self.assertRaises(app_server_client.AppServerError) as raised:
            client.launch("/workspace/repo", "private recovery prompt")

        self.assertNotIn("Bearer", str(raised.exception))
        self.assertNotIn("private recovery prompt", str(raised.exception))
        self.assert_proxy_cleaned_up(process)


if __name__ == "__main__":
    unittest.main()
