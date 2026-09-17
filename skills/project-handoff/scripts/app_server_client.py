from dataclasses import dataclass
import json
import math
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
                    with self._lock:
                        return "".join(self._chunks)
                with self._lock:
                    remaining = self._limit - self._size
                    if remaining > 0:
                        kept = chunk[:remaining]
                        self._chunks.append(kept)
                        self._size += len(kept)
        except (OSError, ValueError):
            with self._lock:
                return "".join(self._chunks)


class AppServerClient:
    def __init__(
        self,
        run_command=subprocess.run,
        popen_factory=subprocess.Popen,
        request_timeout=10.0,
    ):
        try:
            timeout = float(request_timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "request_timeout must be finite and greater than zero"
            ) from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(
                "request_timeout must be finite and greater than zero"
            )
        self._run_command = run_command
        self._popen_factory = popen_factory
        self._request_timeout = timeout

    def launch(self, cwd: str, prompt: str) -> LaunchResult:
        self._start_daemon()
        process = self._start_proxy()
        writes = None
        started_threads = []
        try:
            self._validate_proxy_streams(process)
            responses = queue.Queue()
            writes = queue.Queue()
            writer_thread = threading.Thread(
                target=self._write_stdin,
                args=(process.stdin, writes),
                name="project-handoff-app-server-stdin",
                daemon=True,
            )
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
            writer_thread.start()
            started_threads.append(writer_thread)
            stdout_thread.start()
            started_threads.append(stdout_thread)
            stderr_thread.start()
            started_threads.append(stderr_thread)

            self._request(
                process,
                responses,
                writes,
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
            self._send(
                writes,
                {"method": "initialized", "params": {}},
                deadline=time.monotonic() + self._request_timeout,
            )
            thread_response = self._request(
                process,
                responses,
                writes,
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
                writes,
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
            self._cleanup_proxy(
                process,
                writes,
                started_threads,
            )

    def _start_daemon(self):
        try:
            result = self._run_command(
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
        returncode = getattr(result, "returncode", 0)
        if returncode:
            raise AppServerError(
                "app server daemon failed to start "
                f"with exit status {returncode}"
            )

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
        return process

    @staticmethod
    def _validate_proxy_streams(process):
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise AppServerError("app server proxy streams are unavailable")

    def _request(self, process, responses, writes, request_id, method, params):
        deadline = time.monotonic() + self._request_timeout
        self._send(
            writes,
            {"id": request_id, "method": method, "params": params},
            deadline,
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
    def _send(writes, message, deadline):
        completion = queue.Queue(maxsize=1)
        writes.put((json.dumps(message) + "\n", completion))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            method = message.get("method", "request")
            raise AppServerError(f"{method} timed out while sending")
        try:
            error = completion.get(timeout=remaining)
        except queue.Empty:
            method = message.get("method", "request")
            raise AppServerError(f"{method} timed out while sending") from None
        if error is not None:
            method = message.get("method", "request")
            raise AppServerError(f"{method} could not be sent to the proxy")

    @staticmethod
    def _write_stdin(stream, writes):
        while True:
            job = writes.get()
            if job is None:
                return
            payload, completion = job
            try:
                stream.write(payload)
                stream.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                completion.put(error)
            else:
                completion.put(None)

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

    def _cleanup_proxy(
        self,
        process,
        writes,
        started_threads,
    ):
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=max(self._request_timeout, 0.1))
        except subprocess.TimeoutExpired:
            kill = getattr(process, "kill", None)
            if kill is not None:
                try:
                    kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=max(self._request_timeout, 0.1))
                except (OSError, subprocess.TimeoutExpired):
                    pass
        except OSError:
            pass
        for stream in (
            getattr(process, "stdin", None),
            getattr(process, "stdout", None),
            getattr(process, "stderr", None),
        ):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        if writes is not None:
            writes.put(None)
        for thread in started_threads:
            thread.join(timeout=0.1)
