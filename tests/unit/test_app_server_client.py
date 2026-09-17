import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock


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


class BlockingInput(RecordingInput):
    def __init__(self, block_on_write=1):
        super().__init__()
        self._block_on_write = block_on_write
        self._write_count = 0
        self.write_entered = threading.Event()
        self.release_write = threading.Event()

    def write(self, value):
        self._write_count += 1
        if self._write_count == self._block_on_write:
            self.write_entered.set()
            self.release_write.wait()
        return super().write(value)

    def close(self):
        self.release_write.set()
        super().close()


class TrackedStringIO(io.StringIO):
    def __init__(self, value=""):
        super().__init__(value)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class BlockingOutput:
    def __init__(self, lines):
        self._lines = list(lines)
        self.read_blocked = threading.Event()
        self._unblock = threading.Event()
        self.was_closed = False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        self.read_blocked.set()
        self._unblock.wait()
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


class TerminateTimeoutProcess(FakeProcess):
    def __init__(self, responses):
        super().__init__(responses)
        self.lifecycle = []

    def terminate(self):
        self.lifecycle.append("terminate")
        super().terminate()

    def wait(self, timeout=None):
        self.lifecycle.append("wait")
        if self.lifecycle.count("wait") == 1:
            raise subprocess.TimeoutExpired("codex app-server proxy", timeout)
        self.waited = True
        return 0

    def kill(self):
        self.lifecycle.append("kill")


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

    def test_constructor_rejects_invalid_request_timeouts_before_startup(self):
        external_calls = []

        def run_command(command, **kwargs):
            external_calls.append(("daemon", command))

        def popen_factory(command, **kwargs):
            external_calls.append(("proxy", command))

        for request_timeout in (
            float("nan"),
            float("inf"),
            float("-inf"),
            0,
            -1,
        ):
            with self.subTest(request_timeout=request_timeout):
                with self.assertRaisesRegex(
                    ValueError,
                    "request_timeout must be finite and greater than zero",
                ):
                    AppServerClient(
                        run_command=run_command,
                        popen_factory=popen_factory,
                        request_timeout=request_timeout,
                    )

        self.assertEqual(external_calls, [])

    def test_daemon_start_is_bounded_and_timeout_is_sanitized(self):
        proxy_started = []

        def timeout_daemon(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 0.25)
            raise subprocess.TimeoutExpired(
                command,
                kwargs["timeout"],
                output="Bearer should-not-leak",
            )

        client = AppServerClient(
            run_command=timeout_daemon,
            popen_factory=lambda command, **kwargs: proxy_started.append(command),
            request_timeout=0.25,
        )

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "daemon.*timed out",
        ) as raised:
            client.start_thread("/workspace/repo", before_send=lambda: None)

        self.assertNotIn("Bearer", str(raised.exception))
        self.assertEqual(proxy_started, [])

    def test_start_thread_runs_callback_before_request_can_be_queued(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
            ]
        )
        observed_writes = []

        def before_send():
            observed_writes.append(process.stdin.getvalue())

        thread_id = self.make_client(process).start_thread(
            "/workspace/repo",
            before_send=before_send,
        )

        self.assertEqual(thread_id, "thr-new")
        self.assertEqual(len(observed_writes), 1)
        self.assertNotIn("thread/start", observed_writes[0])
        self.assertIn("thread/start", process.stdin.getvalue())
        self.assert_proxy_cleaned_up(process)

    def test_callback_failure_prevents_thread_request(self):
        process = FakeProcess(
            [{"id": 1, "result": {"capabilities": {}}}]
        )
        callback_error = OSError("durable state unavailable")

        with self.assertRaises(OSError) as raised:
            self.make_client(process).start_thread(
                "/workspace/repo",
                before_send=lambda: (_ for _ in ()).throw(callback_error),
            )

        self.assertIs(raised.exception, callback_error)
        methods = [
            json.loads(line)["method"]
            for line in process.stdin.getvalue().splitlines()
        ]
        self.assertEqual(methods, ["initialize", "initialized"])
        self.assert_proxy_cleaned_up(process)

    def test_start_turn_sends_stable_client_user_message_id(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        callback_calls = []

        turn_id = self.make_client(process).start_turn(
            "thr-new",
            "resume from the handoff",
            "project-handoff:pending-123",
            before_send=lambda: callback_calls.append("persisted"),
        )

        self.assertEqual(turn_id, "turn-new")
        self.assertEqual(callback_calls, ["persisted"])
        messages = [
            json.loads(line) for line in process.stdin.getvalue().splitlines()
        ]
        self.assertEqual(
            messages[-1],
            {
                "id": 2,
                "method": "turn/start",
                "params": {
                    "threadId": "thr-new",
                    "clientUserMessageId": "project-handoff:pending-123",
                    "input": [
                        {
                            "type": "text",
                            "text": "resume from the handoff",
                        }
                    ],
                },
            },
        )
        self.assert_proxy_cleaned_up(process)

    def test_callback_failure_prevents_turn_request(self):
        process = FakeProcess(
            [{"id": 1, "result": {"capabilities": {}}}]
        )
        callback_error = OSError("durable turn state unavailable")

        with self.assertRaises(OSError) as raised:
            self.make_client(process).start_turn(
                "thr-new",
                "resume from the handoff",
                "project-handoff:pending-123",
                before_send=lambda: (_ for _ in ()).throw(callback_error),
            )

        self.assertIs(raised.exception, callback_error)
        methods = [
            json.loads(line)["method"]
            for line in process.stdin.getvalue().splitlines()
        ]
        self.assertEqual(methods, ["initialize", "initialized"])
        self.assert_proxy_cleaned_up(process)

    def test_launch_preserves_worker_start_failure_and_cleans_up(self):
        process = FakeProcess()
        client = self.make_client(process)
        start_error = RuntimeError("stdout worker failed to start")
        created_threads = []

        class ControlledThread:
            def __init__(self, fail_on_start):
                self.fail_on_start = fail_on_start
                self.started = False
                self.join_calls = 0

            def start(self):
                if self.fail_on_start:
                    raise start_error
                self.started = True

            def join(self, timeout=None):
                self.join_calls += 1
                if not self.started:
                    raise RuntimeError("cannot join thread before it is started")

        def thread_factory(**kwargs):
            thread = ControlledThread(fail_on_start=len(created_threads) == 1)
            created_threads.append(thread)
            return thread

        with mock.patch.object(
            app_server_client.threading,
            "Thread",
            side_effect=thread_factory,
        ):
            with self.assertRaises(RuntimeError) as raised:
                client.launch("/workspace/repo", "private recovery prompt")

        self.assertIs(raised.exception, start_error)
        self.assertEqual(len(created_threads), 3)
        self.assertEqual(created_threads[0].join_calls, 1)
        self.assertEqual(created_threads[1].join_calls, 0)
        self.assertEqual(created_threads[2].join_calls, 0)
        self.assert_proxy_cleaned_up(process)

    def test_launch_times_out_when_proxy_stops_consuming_stdin(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {"id": 3, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        process.stdin = BlockingInput()
        client = self.make_client(process, request_timeout=0.01)
        outcome = []

        def launch():
            try:
                client.launch("/workspace/repo", "private recovery prompt")
            except BaseException as error:
                outcome.append(error)
            else:
                outcome.append(None)

        launch_thread = threading.Thread(target=launch, daemon=True)
        launch_thread.start()
        self.assertTrue(process.stdin.write_entered.wait(timeout=1))
        launch_thread.join(timeout=1)
        was_still_blocked = launch_thread.is_alive()
        if was_still_blocked:
            process.stdin.release_write.set()
            launch_thread.join(timeout=1)

        self.assertFalse(was_still_blocked, "launch ignored its write deadline")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], app_server_client.AppServerError)
        self.assertRegex(str(outcome[0]), "initialize.*timed out")
        self.assertNotIn("private recovery prompt", str(outcome[0]))
        self.assert_proxy_cleaned_up(process)

    def test_launch_bounds_initialized_notification_write(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {"id": 3, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        process.stdin = BlockingInput(block_on_write=2)
        client = self.make_client(process, request_timeout=0.01)
        outcome = []

        def launch():
            try:
                client.launch("/workspace/repo", "private recovery prompt")
            except BaseException as error:
                outcome.append(error)
            else:
                outcome.append(None)

        launch_thread = threading.Thread(target=launch, daemon=True)
        launch_thread.start()
        self.assertTrue(process.stdin.write_entered.wait(timeout=1))
        launch_thread.join(timeout=1)
        was_still_blocked = launch_thread.is_alive()
        if was_still_blocked:
            process.stdin.release_write.set()
            launch_thread.join(timeout=1)

        self.assertFalse(was_still_blocked, "initialized write ignored its deadline")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], app_server_client.AppServerError)
        self.assertRegex(str(outcome[0]), "initialized.*timed out")
        self.assertNotIn("private recovery prompt", str(outcome[0]))
        self.assert_proxy_cleaned_up(process)

    def test_launch_cleans_up_proxy_with_missing_stream(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {"id": 3, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        process.stdout = None
        client = self.make_client(process)

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "proxy streams are unavailable",
        ):
            client.launch("/workspace/repo", "private recovery prompt")

        self.assertTrue(process.stdin.was_closed)
        self.assertTrue(process.stderr.was_closed)
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    def test_launch_reaps_proxy_after_terminate_timeout(self):
        process = TerminateTimeoutProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {"id": 3, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        client = self.make_client(process)

        result = client.launch("/workspace/repo", "resume from handoff")

        self.assertEqual(
            result,
            LaunchResult(thread_id="thr-new", turn_id="turn-new"),
        )
        self.assertEqual(
            process.lifecycle,
            ["terminate", "wait", "kill", "wait"],
        )
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
            daemon_commands.append((command, kwargs))

        def popen_factory(command, **kwargs):
            proxy_commands.append((command, kwargs))
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
            [
                (
                    ["codex", "app-server", "daemon", "start"],
                    {
                        "check": True,
                        "capture_output": True,
                        "text": True,
                        "timeout": 1.0,
                    },
                )
            ],
        )
        self.assertEqual(
            proxy_commands,
            [
                (
                    ["codex", "app-server", "proxy"],
                    {
                        "stdin": subprocess.PIPE,
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE,
                        "text": True,
                        "bufsize": 1,
                    },
                )
            ],
        )
        self.assert_proxy_cleaned_up(process)

    def test_launch_rejects_returned_nonzero_daemon_status(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                {"id": 3, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        proxy_started = []

        def return_failure(command, **kwargs):
            return subprocess.CompletedProcess(
                args=command,
                returncode=9,
                stdout="",
                stderr="Bearer should-not-leak",
            )

        def start_proxy(command, **kwargs):
            proxy_started.append(command)
            return process

        client = AppServerClient(
            run_command=return_failure,
            popen_factory=start_proxy,
            request_timeout=1.0,
        )

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "daemon.*9",
        ) as raised:
            client.launch("/workspace/repo", "private recovery prompt")

        self.assertNotIn("Bearer", str(raised.exception))
        self.assertNotIn("private recovery prompt", str(raised.exception))
        self.assertEqual(proxy_started, [])
        self.assertFalse(process.terminated)

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
        stdout = BlockingOutput([initialize_response])
        process = FakeProcess(
            stdout=stdout,
        )
        client = self.make_client(
            process,
            request_timeout=0.01,
        )
        outcome = []

        def launch():
            try:
                client.launch("/workspace/repo", "resume from the handoff")
            except BaseException as error:
                outcome.append(error)
            else:
                outcome.append(None)

        launch_thread = threading.Thread(target=launch, daemon=True)
        launch_thread.start()
        self.assertTrue(stdout.read_blocked.wait(timeout=1))
        launch_thread.join(timeout=1)
        was_still_blocked = launch_thread.is_alive()
        if was_still_blocked:
            stdout.close()
            launch_thread.join(timeout=1)

        self.assertFalse(was_still_blocked, "launch ignored its response deadline")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], app_server_client.AppServerError)
        self.assertRegex(str(outcome[0]), "thread/start.*timed out")
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
        stderr = "Bearer should-not-leak" + "x" * 100_000
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

    def test_stderr_capture_retains_only_bounded_prefix(self):
        stderr = "sensitive-prefix:" + "x" * 10_000
        capture = app_server_client._BoundedCapture(
            limit=app_server_client._STDERR_CAPTURE_LIMIT
        )

        retained = capture.drain(io.StringIO(stderr))

        self.assertEqual(len(retained), 4096)
        self.assertEqual(retained, stderr[:4096])


if __name__ == "__main__":
    unittest.main()
