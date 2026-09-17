import json
import math
import queue
import subprocess
import threading
import time


_STDERR_CAPTURE_LIMIT = 4096


class AppServerError(RuntimeError):
    def __init__(self, message, *, request_may_have_been_sent=True):
        super().__init__(message)
        self.request_may_have_been_sent = bool(
            request_may_have_been_sent
        )


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

    def start_thread(self, cwd: str, before_send) -> str:
        return self._single_operation(
            method="thread/start",
            params={"cwd": cwd},
            object_name="thread",
            before_send=before_send,
        )

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        client_user_message_id: str,
        before_send,
    ) -> str:
        return self._single_operation(
            method="turn/start",
            params={
                "threadId": thread_id,
                "clientUserMessageId": client_user_message_id,
                "input": [{"type": "text", "text": prompt}],
            },
            object_name="turn",
            before_send=before_send,
        )

    def _single_operation(
        self,
        method,
        params,
        object_name,
        before_send,
    ):
        self._start_daemon()
        process = self._start_proxy()
        writes = None
        started_threads = []
        try:
            self._validate_proxy_streams(process)
            responses = queue.Queue()
            writes = queue.Queue()
            worker_specs = (
                (
                    self._write_stdin,
                    (process.stdin, writes),
                    "project-handoff-app-server-stdin",
                ),
                (
                    self._read_stdout,
                    (process.stdout, responses),
                    "project-handoff-app-server-stdout",
                ),
            )
            stderr_capture = _BoundedCapture(_STDERR_CAPTURE_LIMIT)
            worker_specs += (
                (
                    stderr_capture.drain,
                    (process.stderr,),
                    "project-handoff-app-server-stderr",
                ),
            )
            for target, args, name in worker_specs:
                worker = threading.Thread(
                    target=target,
                    args=args,
                    name=name,
                    daemon=True,
                )
                worker.start()
                started_threads.append(worker)

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
            response = self._request(
                process,
                responses,
                writes,
                2,
                method,
                params,
                before_send=before_send,
            )
            return self._result_id(
                response,
                method=method,
                object_name=object_name,
            )
        finally:
            self._cleanup_proxy(process, writes, started_threads)

    def _start_daemon(self):
        try:
            result = self._run_command(
                ["codex", "app-server", "daemon", "start"],
                check=True,
                capture_output=True,
                text=True,
                timeout=self._request_timeout,
            )
        except subprocess.TimeoutExpired:
            raise AppServerError(
                "app server daemon timed out while starting",
                request_may_have_been_sent=False,
            ) from None
        except subprocess.CalledProcessError as error:
            raise AppServerError(
                "app server daemon failed to start "
                f"with exit status {error.returncode}",
                request_may_have_been_sent=False,
            ) from None
        except (OSError, RuntimeError):
            raise AppServerError(
                "app server daemon failed to start",
                request_may_have_been_sent=False,
            ) from None
        returncode = getattr(result, "returncode", 0)
        if returncode:
            raise AppServerError(
                "app server daemon failed to start "
                f"with exit status {returncode}",
                request_may_have_been_sent=False,
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
            raise AppServerError(
                "app server proxy failed to start",
                request_may_have_been_sent=False,
            ) from None
        return process

    @staticmethod
    def _validate_proxy_streams(process):
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise AppServerError(
                "app server proxy streams are unavailable",
                request_may_have_been_sent=False,
            )

    def _request(
        self,
        process,
        responses,
        writes,
        request_id,
        method,
        params,
        before_send=None,
    ):
        if before_send is not None:
            before_send()
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
        method = message.get("method", "request")
        try:
            payload = json.dumps(message) + "\n"
        except (TypeError, ValueError, OverflowError):
            raise AppServerError(
                f"{method} could not be serialized",
                request_may_have_been_sent=False,
            ) from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerError(
                f"{method} timed out before queueing",
                request_may_have_been_sent=False,
            )
        completion = queue.Queue(maxsize=1)
        try:
            writes.put((payload, completion))
        except Exception:
            raise AppServerError(
                f"{method} could not be queued",
            ) from None
        try:
            error = completion.get(timeout=remaining)
        except queue.Empty:
            raise AppServerError(f"{method} timed out while sending") from None
        if error is not None:
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
