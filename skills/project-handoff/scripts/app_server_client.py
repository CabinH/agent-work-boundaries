from dataclasses import dataclass
import json
import queue
import subprocess
import threading
import time


_STDERR_CAPTURE_LIMIT = 4096


class AppServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaunchResult:
    thread_id: str
    turn_id: str


class _BoundedCapture:
    def __init__(self, limit):
        self._limit = limit
        self._chunks = []
        self._size = 0
        self._lock = threading.Lock()

    def drain(self, stream):
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    return
                with self._lock:
                    remaining = self._limit - self._size
                    if remaining > 0:
                        kept = chunk[:remaining]
                        self._chunks.append(kept)
                        self._size += len(kept)
        except (OSError, ValueError):
            return


class AppServerClient:
    def __init__(
        self,
        run_command=subprocess.run,
        popen_factory=subprocess.Popen,
        request_timeout=10.0,
    ):
        self._run_command = run_command
        self._popen_factory = popen_factory
        self._request_timeout = float(request_timeout)

    def launch(self, cwd: str, prompt: str) -> LaunchResult:
        self._start_daemon()
        process = self._start_proxy()
        responses = queue.Queue()
        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, responses),
            name="project-handoff-app-server-stdout",
            daemon=True,
        )
        stderr_capture = _BoundedCapture(_STDERR_CAPTURE_LIMIT)
        stderr_thread = threading.Thread(
            target=stderr_capture.drain,
            args=(process.stderr,),
            name="project-handoff-app-server-stderr",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            self._request(
                process,
                responses,
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "project-handoff",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send(process, {"method": "initialized", "params": {}})
            thread_response = self._request(
                process,
                responses,
                2,
                "thread/start",
                {"cwd": cwd},
            )
            thread_id = self._result_id(
                thread_response,
                method="thread/start",
                object_name="thread",
            )
            turn_response = self._request(
                process,
                responses,
                3,
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
            turn_id = self._result_id(
                turn_response,
                method="turn/start",
                object_name="turn",
            )
            return LaunchResult(thread_id=thread_id, turn_id=turn_id)
        finally:
            self._cleanup_proxy(process, stdout_thread, stderr_thread)

    def _start_daemon(self):
        try:
            self._run_command(
                ["codex", "app-server", "daemon", "start"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as error:
            raise AppServerError(
                "app server daemon failed to start "
                f"with exit status {error.returncode}"
            ) from None
        except (OSError, RuntimeError):
            raise AppServerError("app server daemon failed to start") from None

    def _start_proxy(self):
        try:
            process = self._popen_factory(
                ["codex", "app-server", "proxy"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except (OSError, RuntimeError):
            raise AppServerError("app server proxy failed to start") from None
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise AppServerError("app server proxy streams are unavailable")
        return process

    def _request(self, process, responses, request_id, method, params):
        deadline = time.monotonic() + self._request_timeout
        self._send(
            process,
            {"id": request_id, "method": method, "params": params},
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError(
                    f"{method} timed out waiting for response"
                )
            try:
                event_type, payload = responses.get(timeout=remaining)
            except queue.Empty:
                raise AppServerError(
                    f"{method} timed out waiting for response"
                ) from None

            if event_type == "eof":
                raise AppServerError(
                    f"{method} failed because the proxy closed"
                )
            if event_type == "read_error":
                raise AppServerError(
                    f"{method} failed while reading the proxy response"
                )
            try:
                response = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                raise AppServerError(
                    f"{method} received an invalid JSON response"
                ) from None
            if not isinstance(response, dict):
                raise AppServerError(
                    f"{method} received an invalid JSON-RPC response"
                )
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                code = error.get("code") if isinstance(error, dict) else None
                suffix = f" (code {code})" if code is not None else ""
                raise AppServerError(f"{method} returned an error{suffix}")
            return response

    @staticmethod
    def _result_id(response, method, object_name):
        try:
            result_id = response["result"][object_name]["id"]
        except (KeyError, TypeError):
            result_id = None
        if not isinstance(result_id, str) or not result_id:
            raise AppServerError(
                f"{method} response is missing {object_name} id"
            )
        return result_id

    @staticmethod
    def _send(process, message):
        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            method = message.get("method", "request")
            raise AppServerError(f"{method} could not be sent to the proxy") from None

    @staticmethod
    def _read_stdout(stream, responses):
        try:
            while True:
                line = stream.readline()
                if line == "":
                    responses.put(("eof", None))
                    return
                responses.put(("line", line))
        except (OSError, ValueError) as error:
            responses.put(("read_error", error))

    def _cleanup_proxy(self, process, stdout_thread, stderr_thread):
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=max(self._request_timeout, 0.1))
        except (OSError, subprocess.TimeoutExpired):
            kill = getattr(process, "kill", None)
            if kill is not None:
                try:
                    kill()
                except (OSError, ProcessLookupError):
                    pass
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        stdout_thread.join(timeout=0.1)
        stderr_thread.join(timeout=0.1)
