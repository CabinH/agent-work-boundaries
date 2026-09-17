import io
import json
from pathlib import Path
import queue
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
from app_server_client import AppServerClient


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


class FailingInput(RecordingInput):
    def __init__(self, fail_on_write):
        super().__init__()
        self._fail_on_write = fail_on_write
        self._write_count = 0

    def write(self, value):
        self._write_count += 1
        if self._write_count == self._fail_on_write:
            raise BrokenPipeError("proxy input closed")
        return super().write(value)


class EnqueueThenRaiseQueue(queue.Queue):
    def __init__(self, target_method):
        super().__init__()
        self.target_method = target_method
        self.inserted_target = False

    def put(self, item, *args, **kwargs):
        super().put(item, *args, **kwargs)
        if item is None:
            return
        payload, _completion = item
        message = json.loads(payload)
        if message.get("method") == self.target_method:
            self.inserted_target = True
            raise RuntimeError("queue raised after insertion")


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

    def test_client_does_not_expose_combined_launch_operation(self):
        process = FakeProcess()
        client = self.make_client(process)

        self.assertFalse(hasattr(client, "launch"))
        self.assertFalse(hasattr(app_server_client, "LaunchResult"))

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

    def test_thread_callback_time_does_not_consume_request_timeout(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
            ]
        )
        clock = [10.0]

        def persist_phase():
            clock[0] += 2.0

        with mock.patch.object(
            app_server_client.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ):
            thread_id = self.make_client(
                process,
                request_timeout=1.0,
            ).start_thread(
                "/workspace/repo",
                before_send=persist_phase,
            )

        self.assertEqual(thread_id, "thr-new")
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

    def test_turn_callback_time_does_not_consume_request_timeout(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"turn": {"id": "turn-new"}}},
            ]
        )
        clock = [10.0]

        def persist_phase():
            clock[0] += 2.0

        with mock.patch.object(
            app_server_client.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ):
            turn_id = self.make_client(
                process,
                request_timeout=1.0,
            ).start_turn(
                "thr-new",
                "resume from the handoff",
                "project-handoff:pending-123",
                before_send=persist_phase,
            )

        self.assertEqual(turn_id, "turn-new")
        self.assertIn("turn/start", process.stdin.getvalue())
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

    def test_target_serialization_failure_is_definitely_unsent(self):
        operations = (
            (
                "thread/start",
                lambda client, callback: client.start_thread(
                    "/workspace/repo",
                    before_send=callback,
                ),
            ),
            (
                "turn/start",
                lambda client, callback: client.start_turn(
                    "thr-new",
                    "resume from the handoff",
                    "project-handoff:pending-123",
                    before_send=callback,
                ),
            ),
        )
        real_dumps = json.dumps

        for target_method, operation in operations:
            with self.subTest(method=target_method):
                process = FakeProcess(
                    [{"id": 1, "result": {"capabilities": {}}}]
                )
                callbacks = []

                def fail_target(message):
                    if message.get("method") == target_method:
                        raise TypeError("not serializable")
                    return real_dumps(message)

                with mock.patch.object(
                    app_server_client.json,
                    "dumps",
                    side_effect=fail_target,
                ):
                    with self.assertRaises(
                        app_server_client.AppServerError
                    ) as raised:
                        operation(
                            self.make_client(process),
                            lambda: callbacks.append("persisted"),
                        )

                self.assertEqual(callbacks, ["persisted"])
                self.assertFalse(
                    raised.exception.request_may_have_been_sent
                )
                methods = [
                    json.loads(line)["method"]
                    for line in process.stdin.getvalue().splitlines()
                ]
                self.assertEqual(methods, ["initialize", "initialized"])
                self.assert_proxy_cleaned_up(process)

    def test_target_writer_timeout_is_conservatively_sent(self):
        operations = (
            (
                "thread/start",
                lambda client, callback: client.start_thread(
                    "/workspace/repo",
                    before_send=callback,
                ),
            ),
            (
                "turn/start",
                lambda client, callback: client.start_turn(
                    "thr-new",
                    "resume from the handoff",
                    "project-handoff:pending-123",
                    before_send=callback,
                ),
            ),
        )

        for target_method, operation in operations:
            with self.subTest(method=target_method):
                process = FakeProcess(
                    [{"id": 1, "result": {"capabilities": {}}}]
                )
                process.stdin = BlockingInput(block_on_write=3)
                callback_called = threading.Event()
                outcome = []

                def run_operation():
                    try:
                        operation(
                            self.make_client(process, request_timeout=0.01),
                            callback_called.set,
                        )
                    except BaseException as error:
                        outcome.append(error)

                worker = threading.Thread(target=run_operation, daemon=True)
                worker.start()
                self.assertTrue(callback_called.wait(timeout=1))
                self.assertTrue(process.stdin.write_entered.wait(timeout=1))
                worker.join(timeout=1)
                if worker.is_alive():
                    process.stdin.release_write.set()
                    worker.join(timeout=1)

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcome), 1)
                self.assertIsInstance(
                    outcome[0],
                    app_server_client.AppServerError,
                )
                self.assertRegex(str(outcome[0]), f"{target_method}.*timed out")
                self.assertTrue(outcome[0].request_may_have_been_sent)
                self.assert_proxy_cleaned_up(process)

    def test_pre_put_deadline_failure_is_definitely_unsent(self):
        writes = queue.Queue()
        with mock.patch.object(
            app_server_client.time,
            "monotonic",
            return_value=11.0,
        ):
            with self.assertRaises(app_server_client.AppServerError) as raised:
                AppServerClient._send(
                    writes,
                    {
                        "id": 2,
                        "method": "thread/start",
                        "params": {"cwd": "/workspace/repo"},
                    },
                    deadline=10.0,
                )

        self.assertFalse(raised.exception.request_may_have_been_sent)
        self.assertTrue(writes.empty())

    def test_queue_insertion_exception_is_conservatively_sent(self):
        writes = mock.Mock(
            put=mock.Mock(side_effect=RuntimeError("queue outcome unknown"))
        )

        with self.assertRaises(app_server_client.AppServerError) as raised:
            AppServerClient._send(
                writes,
                {
                    "id": 2,
                    "method": "thread/start",
                    "params": {"cwd": "/workspace/repo"},
                },
                deadline=app_server_client.time.monotonic() + 1.0,
            )

        self.assertTrue(raised.exception.request_may_have_been_sent)

    def test_enqueue_then_raise_is_conservatively_sent(self):
        operations = (
            (
                "thread/start",
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
                lambda client: client.start_thread(
                    "/workspace/repo",
                    before_send=lambda: None,
                ),
            ),
            (
                "turn/start",
                {"id": 2, "result": {"turn": {"id": "turn-new"}}},
                lambda client: client.start_turn(
                    "thr-new",
                    "resume from the handoff",
                    "project-handoff:pending-123",
                    before_send=lambda: None,
                ),
            ),
        )
        real_queue = queue.Queue

        for target_method, target_response, operation in operations:
            with self.subTest(method=target_method):
                process = FakeProcess(
                    [
                        {"id": 1, "result": {"capabilities": {}}},
                        target_response,
                    ]
                )
                writes = EnqueueThenRaiseQueue(target_method)
                queue_count = 0

                def queue_factory(*args, **kwargs):
                    nonlocal queue_count
                    queue_count += 1
                    if queue_count == 2:
                        return writes
                    return real_queue(*args, **kwargs)

                with mock.patch.object(
                    app_server_client.queue,
                    "Queue",
                    side_effect=queue_factory,
                ):
                    with self.assertRaises(
                        app_server_client.AppServerError
                    ) as raised:
                        operation(self.make_client(process))

                self.assertTrue(writes.inserted_target)
                self.assertTrue(
                    raised.exception.request_may_have_been_sent
                )
                self.assert_proxy_cleaned_up(process)

    def test_target_writer_error_is_conservatively_sent(self):
        process = FakeProcess(
            [{"id": 1, "result": {"capabilities": {}}}]
        )
        process.stdin = FailingInput(fail_on_write=3)
        client = self.make_client(process)

        with self.assertRaises(app_server_client.AppServerError) as raised:
            client.start_thread(
                "/workspace/repo",
                before_send=lambda: None,
            )

        self.assertRegex(str(raised.exception), "thread/start.*could not be sent")
        self.assertTrue(raised.exception.request_may_have_been_sent)
        self.assert_proxy_cleaned_up(process)

    def test_target_response_failures_are_conservatively_sent(self):
        initialize = json.dumps(
            {"id": 1, "result": {"capabilities": {}}}
        ) + "\n"
        cases = (
            ("eof", initialize, "proxy closed"),
            ("invalid", initialize + "not-json\n", "invalid JSON"),
            (
                "error",
                initialize
                + json.dumps(
                    {
                        "id": 2,
                        "error": {"code": -32000, "message": "private"},
                    }
                )
                + "\n",
                "returned an error",
            ),
        )

        for label, stdout, expected in cases:
            with self.subTest(failure=label):
                process = FakeProcess(stdout=TrackedStringIO(stdout))
                client = self.make_client(process)

                with self.assertRaisesRegex(
                    app_server_client.AppServerError,
                    expected,
                ) as raised:
                    client.start_thread(
                        "/workspace/repo",
                        before_send=lambda: None,
                    )

                self.assertTrue(
                    raised.exception.request_may_have_been_sent
                )
                self.assertNotIn("private", str(raised.exception))
                self.assert_proxy_cleaned_up(process)

    def test_split_operation_preserves_worker_start_failure_and_cleans_up(self):
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
                client.start_thread(
                    "/workspace/repo",
                    before_send=lambda: None,
                )

        self.assertIs(raised.exception, start_error)
        self.assertEqual(len(created_threads), 2)
        self.assertEqual(created_threads[0].join_calls, 1)
        self.assertEqual(created_threads[1].join_calls, 0)
        self.assert_proxy_cleaned_up(process)

    def test_start_thread_times_out_when_proxy_stops_consuming_stdin(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
            ]
        )
        process.stdin = BlockingInput()
        client = self.make_client(process, request_timeout=0.01)
        outcome = []

        def start_thread():
            try:
                client.start_thread(
                    "/workspace/repo",
                    before_send=lambda: None,
                )
            except BaseException as error:
                outcome.append(error)
            else:
                outcome.append(None)

        operation_thread = threading.Thread(target=start_thread, daemon=True)
        operation_thread.start()
        self.assertTrue(process.stdin.write_entered.wait(timeout=1))
        operation_thread.join(timeout=1)
        was_still_blocked = operation_thread.is_alive()
        if was_still_blocked:
            process.stdin.release_write.set()
            operation_thread.join(timeout=1)

        self.assertFalse(
            was_still_blocked,
            "start_thread ignored its write deadline",
        )
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], app_server_client.AppServerError)
        self.assertRegex(str(outcome[0]), "initialize.*timed out")
        self.assert_proxy_cleaned_up(process)

    def test_start_thread_bounds_initialized_notification_write(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
            ]
        )
        process.stdin = BlockingInput(block_on_write=2)
        client = self.make_client(process, request_timeout=0.01)
        outcome = []

        def start_thread():
            try:
                client.start_thread(
                    "/workspace/repo",
                    before_send=lambda: None,
                )
            except BaseException as error:
                outcome.append(error)
            else:
                outcome.append(None)

        operation_thread = threading.Thread(target=start_thread, daemon=True)
        operation_thread.start()
        self.assertTrue(process.stdin.write_entered.wait(timeout=1))
        operation_thread.join(timeout=1)
        was_still_blocked = operation_thread.is_alive()
        if was_still_blocked:
            process.stdin.release_write.set()
            operation_thread.join(timeout=1)

        self.assertFalse(was_still_blocked, "initialized write ignored its deadline")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], app_server_client.AppServerError)
        self.assertRegex(str(outcome[0]), "initialized.*timed out")
        self.assert_proxy_cleaned_up(process)

    def test_start_thread_cleans_up_proxy_with_missing_stream(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
            ]
        )
        process.stdout = None
        client = self.make_client(process)

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "proxy streams are unavailable",
        ):
            client.start_thread(
                "/workspace/repo",
                before_send=lambda: None,
            )

        self.assertTrue(process.stdin.was_closed)
        self.assertTrue(process.stderr.was_closed)
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    def test_start_thread_reaps_proxy_after_terminate_timeout(self):
        process = TerminateTimeoutProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
            ]
        )
        client = self.make_client(process)

        result = client.start_thread(
            "/workspace/repo",
            before_send=lambda: None,
        )

        self.assertEqual(result, "thr-new")
        self.assertEqual(
            process.lifecycle,
            ["terminate", "wait", "kill", "wait"],
        )
        self.assertTrue(process.waited)

    def test_start_thread_starts_daemon_and_proxy(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
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

        result = client.start_thread(
            cwd="/workspace/repo",
            before_send=lambda: None,
        )

        self.assertEqual(result, "thr-new")
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

    def test_start_thread_rejects_returned_nonzero_daemon_status(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
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
            client.start_thread(
                "/workspace/repo",
                before_send=lambda: None,
            )

        self.assertNotIn("Bearer", str(raised.exception))
        self.assertEqual(proxy_started, [])
        self.assertFalse(process.terminated)

    def test_start_thread_surfaces_daemon_start_failure(self):
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
            client.start_thread(
                "/workspace/repo",
                before_send=lambda: None,
            )

        self.assertNotIn("Bearer", str(raised.exception))
        self.assertEqual(proxy_started, [])
        self.assertFalse(process.terminated)

    def test_start_thread_rejects_json_rpc_error(self):
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
            client.start_thread(
                "/workspace/repo",
                before_send=lambda: None,
            )

        self.assertNotIn("Bearer", str(raised.exception))
        self.assert_proxy_cleaned_up(process)

    def test_start_thread_rejects_missing_thread_id(self):
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
        ) as raised:
            client.start_thread(
                "/workspace/repo",
                before_send=lambda: None,
            )

        self.assertTrue(raised.exception.request_may_have_been_sent)
        self.assert_proxy_cleaned_up(process)

    def test_start_turn_rejects_missing_turn_id(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"capabilities": {}}},
                {"id": 2, "result": {"turn": {}}},
            ]
        )
        client = self.make_client(process)

        with self.assertRaisesRegex(
            app_server_client.AppServerError,
            "turn/start.*turn id",
        ):
            client.start_turn(
                "thr-new",
                "resume from the handoff",
                "project-handoff:pending-123",
                before_send=lambda: None,
            )

        self.assert_proxy_cleaned_up(process)

    def test_start_thread_times_out_waiting_for_response(self):
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

        def start_thread():
            try:
                client.start_thread(
                    "/workspace/repo",
                    before_send=lambda: None,
                )
            except BaseException as error:
                outcome.append(error)
            else:
                outcome.append(None)

        operation_thread = threading.Thread(target=start_thread, daemon=True)
        operation_thread.start()
        self.assertTrue(stdout.read_blocked.wait(timeout=1))
        operation_thread.join(timeout=1)
        was_still_blocked = operation_thread.is_alive()
        if was_still_blocked:
            stdout.close()
            operation_thread.join(timeout=1)

        self.assertFalse(
            was_still_blocked,
            "start_thread ignored its response deadline",
        )
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], app_server_client.AppServerError)
        self.assertRegex(str(outcome[0]), "thread/start.*timed out")
        self.assertTrue(outcome[0].request_may_have_been_sent)
        self.assert_proxy_cleaned_up(process)

    def test_thread_notifications_do_not_consume_response_ids(self):
        process = FakeProcess(
            [
                {"method": "server/ready", "params": {}},
                {"id": 1, "result": {"capabilities": {}}},
                {
                    "method": "thread/started",
                    "params": {"thread": {"id": "thr-new"}},
                },
                {"id": 2, "result": {"thread": {"id": "thr-new"}}},
            ]
        )
        client = self.make_client(process)

        result = client.start_thread(
            "/workspace/repo",
            before_send=lambda: None,
        )

        self.assertEqual(result, "thr-new")

    def test_start_thread_bounds_proxy_stderr_on_malformed_response(self):
        stderr = "Bearer should-not-leak" + "x" * 100_000
        process = FakeProcess(
            stdout=TrackedStringIO("not-json\n"),
            stderr=stderr,
        )
        client = self.make_client(process)

        with self.assertRaises(app_server_client.AppServerError) as raised:
            client.start_thread(
                "/workspace/repo",
                before_send=lambda: None,
            )

        self.assertNotIn("Bearer", str(raised.exception))
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
