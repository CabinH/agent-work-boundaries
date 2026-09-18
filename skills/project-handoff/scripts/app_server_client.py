import base64
import hashlib
import json
import math
import os
import queue
import select
import struct
import subprocess
import threading
import time


_STDERR_CAPTURE_LIMIT = 4096
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024 * 1024
_DAEMON_RESTART_TIMEOUT_SECONDS = 90.0


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
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                with self._lock:
                    remaining = self._limit - self._size
                    if remaining > 0:
                        kept = chunk[:remaining]
                        self._chunks.append(kept)
                        self._size += len(kept)
        except (OSError, ValueError):
            with self._lock:
                return "".join(self._chunks)


class _WebSocketProxyTransport:
    def __init__(self, process, handshake_timeout):
        self._input = process.stdin
        self._output = process.stdout
        self._handshake_timeout = handshake_timeout
        self._write_lock = threading.Lock()

    def open(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        try:
            self._input.write(request)
            self._input.flush()
            response = self._read_headers(
                time.monotonic() + self._handshake_timeout
            )
        except (EOFError, OSError, TimeoutError, ValueError):
            raise AppServerError(
                "app server proxy WebSocket handshake failed",
                request_may_have_been_sent=False,
            ) from None

        lines = response.decode("latin-1").split("\r\n")
        if not lines or not lines[0].startswith("HTTP/1.1 101 "):
            raise AppServerError(
                "app server proxy rejected the WebSocket handshake",
                request_may_have_been_sent=False,
            )
        try:
            headers = {
                name.strip().lower(): value.strip()
                for name, value in (
                    line.split(":", 1) for line in lines[1:] if ":" in line
                )
            }
            expected = base64.b64encode(
                hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
            ).decode("ascii")
        except (TypeError, ValueError):
            raise AppServerError(
                "app server proxy returned an invalid WebSocket handshake",
                request_may_have_been_sent=False,
            ) from None
        if headers.get("sec-websocket-accept") != expected:
            raise AppServerError(
                "app server proxy returned an invalid WebSocket handshake",
                request_may_have_been_sent=False,
            )

    def write_message(self, payload):
        self._write_frame(0x01, payload.encode("utf-8"))

    def read_message(self):
        fragments = []
        total = 0
        expecting_continuation = False
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            if first & 0x70:
                raise ValueError("WebSocket reserved bits were set")
            if second & 0x80:
                raise ValueError("server WebSocket frame was masked")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > _MAX_WEBSOCKET_MESSAGE_BYTES - total:
                raise ValueError("WebSocket message exceeded the size limit")
            payload = self._read_exact(length)

            if opcode == 0x08:
                raise EOFError("WebSocket closed")
            if opcode == 0x09:
                if not final or length > 125:
                    raise ValueError("invalid WebSocket ping frame")
                self._write_frame(0x0A, payload)
                continue
            if opcode == 0x0A:
                if not final or length > 125:
                    raise ValueError("invalid WebSocket pong frame")
                continue
            if opcode == 0x01:
                if expecting_continuation:
                    raise ValueError("unexpected WebSocket text frame")
                fragments = [payload]
                total = length
                expecting_continuation = not final
            elif opcode == 0x00:
                if not expecting_continuation:
                    raise ValueError("unexpected WebSocket continuation frame")
                fragments.append(payload)
                total += length
                expecting_continuation = not final
            else:
                raise ValueError("unexpected WebSocket frame type")
            if final:
                try:
                    return b"".join(fragments).decode("utf-8")
                except UnicodeDecodeError:
                    raise ValueError("invalid WebSocket text payload") from None

    def _read_headers(self, deadline):
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            if len(response) >= 64 * 1024:
                raise ValueError("WebSocket response headers are too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WebSocket handshake timed out")
            ready, _, _ = select.select([self._output], [], [], remaining)
            if not ready:
                raise TimeoutError("WebSocket handshake timed out")
            response.extend(self._read_exact(1))
        return bytes(response[:-4])

    def _read_exact(self, count):
        chunks = []
        remaining = count
        while remaining:
            chunk = self._output.read(remaining)
            if not chunk:
                raise EOFError("proxy closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _write_frame(self, opcode, payload):
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", length)
        masked = bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(payload)
        )
        with self._write_lock:
            self._input.write(header + mask + masked)
            self._input.flush()


class AppServerClient:
    def __init__(
        self,
        run_command=subprocess.run,
        popen_factory=subprocess.Popen,
        request_timeout=10.0,
        daemon_lock_factory=None,
        transport_factory=None,
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
        if daemon_lock_factory is None or not callable(daemon_lock_factory):
            raise ValueError("daemon_lock_factory is required")
        self._daemon_lock_factory = daemon_lock_factory
        self._transport_factory = (
            transport_factory or _WebSocketProxyTransport
        )

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
        with self._daemon_lock_factory():
            self._ensure_compatible_daemon()
            return self._connected_operation(
                method,
                params,
                object_name,
                before_send,
            )

    def _connected_operation(
        self,
        method,
        params,
        object_name,
        before_send,
    ):
        process = self._start_proxy()
        transport = None
        writes = None
        started_threads = []
        try:
            self._validate_proxy_streams(process)
            transport = self._transport_factory(
                process,
                self._request_timeout,
            )
            transport.open()
            responses = queue.Queue()
            writes = queue.Queue()
            worker_specs = (
                (
                    self._write_stdin,
                    (transport, writes),
                    "project-handoff-app-server-stdin",
                ),
                (
                    self._read_stdout,
                    (transport, responses),
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

    def _ensure_compatible_daemon(self):
        self._run_daemon_command("start")
        cli_version, app_server_version = self._daemon_versions()
        if cli_version == app_server_version:
            return
        self._run_daemon_command("restart")
        cli_version, app_server_version = self._daemon_versions()
        if cli_version != app_server_version:
            raise AppServerError(
                "app server daemon versions remained mismatched after restart",
                request_may_have_been_sent=False,
            )

    def _daemon_versions(self):
        result = self._run_daemon_command("version")
        try:
            payload = json.loads(result.stdout)
        except (AttributeError, TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            raise AppServerError(
                "app server daemon version check returned invalid data",
                request_may_have_been_sent=False,
            )
        cli_version = payload.get("cliVersion")
        app_server_version = payload.get("appServerVersion")
        if (
            payload.get("status") != "running"
            or not isinstance(cli_version, str)
            or not cli_version
            or not isinstance(app_server_version, str)
            or not app_server_version
        ):
            raise AppServerError(
                "app server daemon version check returned invalid data",
                request_may_have_been_sent=False,
            )
        return cli_version, app_server_version

    def _run_daemon_command(self, action):
        activity = {
            "start": "starting",
            "restart": "restarting",
            "version": "checking versions",
        }[action]
        failure_action = {
            "start": "start",
            "restart": "restart",
            "version": "check versions",
        }[action]
        timeout = (
            _DAEMON_RESTART_TIMEOUT_SECONDS
            if action == "restart"
            else self._request_timeout
        )
        try:
            result = self._run_command(
                ["codex", "app-server", "daemon", action],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise AppServerError(
                f"app server daemon timed out while {activity}",
                request_may_have_been_sent=False,
            ) from None
        except subprocess.CalledProcessError as error:
            raise AppServerError(
                f"app server daemon failed to {failure_action} "
                f"with exit status {error.returncode}",
                request_may_have_been_sent=False,
            ) from None
        except (OSError, RuntimeError):
            raise AppServerError(
                f"app server daemon failed to {failure_action}",
                request_may_have_been_sent=False,
            ) from None
        returncode = getattr(result, "returncode", 0)
        if returncode:
            raise AppServerError(
                f"app server daemon failed to {failure_action} "
                f"with exit status {returncode}",
                request_may_have_been_sent=False,
            )
        return result

    def _start_proxy(self):
        try:
            process = self._popen_factory(
                ["codex", "app-server", "proxy"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
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
            payload = json.dumps(message)
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
    def _write_stdin(transport, writes):
        while True:
            job = writes.get()
            if job is None:
                return
            payload, completion = job
            try:
                transport.write_message(payload)
            except (BrokenPipeError, OSError, TypeError, ValueError) as error:
                completion.put(error)
            else:
                completion.put(None)

    @staticmethod
    def _read_stdout(transport, responses):
        try:
            while True:
                responses.put(("line", transport.read_message()))
        except EOFError:
            responses.put(("eof", None))
        except (OSError, TypeError, ValueError) as error:
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
