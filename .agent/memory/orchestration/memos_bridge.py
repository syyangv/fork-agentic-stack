"""Bounded JSON-RPC stdio client for the pinned MemOS local bridge.

The upstream plugin does not expose a capabilities RPC.  Capabilities are
therefore tied to the reviewed artifact version here rather than inferred at
runtime.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PINNED_MEMOS_VERSION = "2.0.10"
_UPSTREAM_CONSOLE_LOG = re.compile(
    rb"^\d{2}:\d{2}:\d{2}\.\d{3} (?:TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s"
)
_REVERSE_REQUEST_ID = re.compile(r"^srv-[1-9][0-9]*$")
ReverseRequestHandler = Callable[[Any], Any]
PINNED_CAPABILITIES = (
    "core.health",
    "core.shutdown",
    "session.open",
    "session.close",
    "episode.open",
    "episode.close",
    "turn.start",
    "turn.end",
    "feedback.submit",
    "memory.search",
    "memory.get_trace",
    "memory.get_policy",
    "memory.get_world",
    "memory.list_episodes",
    "memory.timeline",
    "memory.list_traces",
    "memory.list_world_models",
    "skill.list",
    "skill.get",
    "skill.archive",
    "retrieval.query",
    "subagent.record",
)


class MemOSBridgeError(RuntimeError):
    """Base class for failures crossing the MemOS process boundary."""

    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class MemOSUnavailableError(MemOSBridgeError):
    """The bridge cannot currently be started or its circuit is open."""


class MemOSTimeoutError(MemOSBridgeError):
    """A written request did not complete within its deadline."""


class MemOSTransportError(MemOSBridgeError):
    """The stdio connection failed after a request may have been delivered."""


class MemOSProtocolError(MemOSBridgeError):
    """The peer emitted invalid or unbounded JSON-RPC."""


class MemOSVersionError(MemOSBridgeError):
    """The running bridge does not match the reviewed artifact version."""


class MemOSUpstreamError(MemOSBridgeError):
    """A well-formed JSON-RPC error returned by MemOS."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"MemOS error {code}: {message}", ambiguous=False)
        self.code = code
        self.data = data


@dataclass(frozen=True)
class BridgeConfig:
    command: Sequence[str]
    home: str | None = None
    memos_home: str | None = None
    memos_config_file: str | None = None
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None
    inherit_environment: bool = False
    call_timeout: float = 2.0
    shutdown_timeout: float = 0.5
    circuit_cooldown: float = 5.0
    max_line_bytes: int = 1024 * 1024
    stderr_history_bytes: int = 16 * 1024
    request_handlers: Mapping[str, ReverseRequestHandler] | None = None
    request_timeout: float = 45.0
    max_request_handlers: int = 1

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("bridge command must not be empty")
        if (
            self.call_timeout <= 0
            or self.shutdown_timeout <= 0
            or self.request_timeout <= 0
        ):
            raise ValueError("bridge timeouts must be positive")
        if self.circuit_cooldown < 0:
            raise ValueError("circuit cooldown must not be negative")
        if self.max_line_bytes < 64:
            raise ValueError("maximum line size must be at least 64 bytes")
        if self.max_request_handlers < 1:
            raise ValueError("maximum reverse request handlers must be positive")
        for method, handler in (self.request_handlers or {}).items():
            if method != "host.llm.complete":
                raise ValueError(f"reverse request method is not allowed: {method!r}")
            if not callable(handler):
                raise ValueError(f"reverse request handler for {method!r} is not callable")


class MemOSBridgeClient:
    """Thread-safe, lazy subprocess client with one policy-controlled replay."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._writer_threads: set[threading.Thread] = set()
        self._request_threads: set[threading.Thread] = set()
        self._request_slots = threading.BoundedSemaphore(config.max_request_handlers)
        self._response_slots = threading.BoundedSemaphore(config.max_request_handlers)
        self._stderr = deque()  # type: deque[bytes]
        self._stderr_size = 0
        self._closed = False
        self._circuit_until = 0.0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def recent_stderr(self) -> str:
        with self._state_lock:
            return b"".join(self._stderr).decode("utf-8", errors="replace")

    def __enter__(self) -> "MemOSBridgeClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        timeout: float | None = None,
        retryable: bool = False,
    ) -> Any:
        """Call a bridge method; replay at most once only when authorized."""
        if not method:
            raise ValueError("method must not be empty")
        budget = self.config.call_timeout if timeout is None else timeout
        if budget <= 0:
            raise ValueError("timeout must be positive")

        # Serializing operations makes restart atomic and keeps ambiguous calls
        # from being disrupted by another thread's recovery.
        with self._operation_lock:
            if self._closed:
                raise MemOSUnavailableError("MemOS bridge client is closed")
            if time.monotonic() < self._circuit_until:
                raise MemOSUnavailableError("MemOS bridge circuit is cooling down")

            attempts = 2 if retryable else 1
            expires_at = time.monotonic() + budget
            last_error: MemOSBridgeError | None = None
            for attempt in range(attempts):
                remaining = expires_at - time.monotonic()
                if remaining <= 0:
                    raise MemOSTimeoutError(
                        f"MemOS method {method!r} exceeded total {budget:.3f}s budget",
                        ambiguous=bool(last_error and last_error.ambiguous),
                    )
                try:
                    result = self._call_once(method, params, remaining)
                    self._circuit_until = 0.0
                    return result
                except (MemOSUnavailableError, MemOSTimeoutError, MemOSTransportError) as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        self._restart()
                        continue
                    self._circuit_until = time.monotonic() + self.config.circuit_cooldown
                    raise exc
            raise AssertionError("unreachable")

    def health(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Validate core.health and attach artifact-pinned capabilities."""
        result = self.call("core.health", timeout=timeout, retryable=True)
        if not isinstance(result, Mapping):
            raise MemOSProtocolError("core.health result must be an object")
        version = result.get("version")
        if version != PINNED_MEMOS_VERSION:
            raise MemOSVersionError(
                f"expected MemOS {PINNED_MEMOS_VERSION}, received {version!r}"
            )
        health = dict(result)
        health["capabilities"] = PINNED_CAPABILITIES
        return health

    def close(self, *, deadline: float | None = None) -> None:
        """Request cooperative shutdown, then terminate only if necessary."""
        lock_timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        acquired = (
            self._operation_lock.acquire()
            if lock_timeout is None else self._operation_lock.acquire(timeout=lock_timeout)
        )
        if not acquired:
            process = self._process
            if process is not None:
                self._stop_process(process, timeout=0.0)
            self._closed = True
            self._process = None
            self._fail_all(
                MemOSUnavailableError("MemOS bridge client closed"), process,
            )
            return
        try:
            if self._closed:
                return
            process = self._process
            if process is not None and process.poll() is None:
                remaining = (
                    self.config.shutdown_timeout if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if remaining > 0 and process.stdin is not None and not process.stdin.closed:
                    try:
                        self._call_once(
                            "core.shutdown", None,
                            min(self.config.shutdown_timeout, remaining),
                        )
                    except MemOSBridgeError:
                        pass
                self._close_stdin(process)
                remaining = (
                    self.config.shutdown_timeout if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                try:
                    process.wait(timeout=min(self.config.shutdown_timeout, remaining))
                except subprocess.TimeoutExpired:
                    self._stop_process(
                        process, timeout=(
                            None if deadline is None
                            else max(0.0, deadline - time.monotonic())
                        ),
                    )
            self._join_writers(timeout=(
                None if deadline is None else max(0.0, deadline - time.monotonic())
            ))
            self._closed = True
            self._process = None
            self._fail_all(MemOSUnavailableError("MemOS bridge client closed"), process)
        finally:
            self._operation_lock.release()

    def _call_once(self, method: str, params: Any, timeout: float) -> Any:
        # Preserve part of the externally visible budget for hard-stop cleanup.
        abort_timeout = min(self.config.shutdown_timeout, 0.05)
        cleanup_reserve = min(timeout / 2, abort_timeout * 2)
        expires_at = time.monotonic() + timeout - cleanup_reserve
        process = self._ensure_process()
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        try:
            payload = json.dumps(
                request, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise MemOSProtocolError(f"request is not JSON serializable: {exc}") from exc
        if len(payload) > self.config.max_line_bytes:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise MemOSProtocolError("MemOS request exceeds maximum line size")

        try:
            self._write_payload(process, payload, expires_at)
        except MemOSBridgeError:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise

        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            with self._state_lock:
                self._pending.pop(request_id, None)
            self._stop_process(process)
            raise MemOSTimeoutError(
                f"MemOS method {method!r} exceeded {timeout:.3f}s", ambiguous=True
            )
        try:
            response = response_queue.get(timeout=remaining)
        except queue.Empty as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            self._stop_process(process)
            raise MemOSTimeoutError(
                f"MemOS method {method!r} exceeded {timeout:.3f}s", ambiguous=True
            ) from exc
        if isinstance(response, BaseException):
            raise response
        if "error" in response:
            error = response["error"]
            raise MemOSUpstreamError(error["code"], error["message"], error.get("data"))
        return response["result"]

    def _write_payload(
        self, process: subprocess.Popen[bytes], payload: bytes, expires_at: float,
    ) -> None:
        """Write a complete frame without allowing pipe backpressure to hang a call."""
        outcome: queue.Queue[tuple[int, BaseException | None]] = queue.Queue(maxsize=1)

        def write() -> None:
            written = 0
            error: BaseException | None = None
            try:
                with self._write_lock:
                    if process.poll() is not None or process.stdin is None:
                        raise BrokenPipeError("bridge process exited")
                    view = memoryview(payload)
                    while written < len(payload):
                        count = process.stdin.write(view[written:])
                        if not isinstance(count, int) or count <= 0:
                            raise BrokenPipeError("bridge stdin accepted no bytes")
                        written += count
                    process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                error = exc
            finally:
                try:
                    outcome.put_nowait((written, error))
                except queue.Full:
                    pass
                with self._state_lock:
                    self._writer_threads.discard(threading.current_thread())

        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            raise MemOSTimeoutError(
                "MemOS deadline expired before writing request", ambiguous=False
            )
        writer = threading.Thread(target=write, name="memos-stdin", daemon=True)
        with self._state_lock:
            self._writer_threads.add(writer)
        try:
            writer.start()
        except RuntimeError:
            with self._state_lock:
                self._writer_threads.discard(writer)
            raise
        try:
            written, error = outcome.get(timeout=remaining)
        except queue.Empty as exc:
            # Once the OS write is in flight it may have partially reached the
            # peer even though the writer cannot yet report a byte count.
            self._stop_process(process)
            raise MemOSTimeoutError(
                "MemOS deadline expired while writing request", ambiguous=True
            ) from exc
        if error is not None:
            raise MemOSTransportError(
                f"failed writing to MemOS bridge: {error}", ambiguous=written > 0
            ) from error

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        with self._state_lock:
            if self._closed:
                raise MemOSUnavailableError("MemOS bridge client is closed")
            if self._process is not None and self._process.poll() is None:
                return self._process
            # The behavioral subprocess must not inherit API keys, tokens, or
            # unrelated harness credentials. Callers opt in explicitly if a
            # legacy environment is ever required.
            env = os.environ.copy() if self.config.inherit_environment else {}
            if self.config.env:
                env.update({str(k): str(v) for k, v in self.config.env.items()})
            for key, value in (
                ("HOME", self.config.home),
                ("MEMOS_HOME", self.config.memos_home),
                ("MEMOS_CONFIG_FILE", self.config.memos_config_file),
            ):
                if value is not None:
                    env[key] = value
            try:
                process = subprocess.Popen(
                    tuple(self.config.command),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.config.cwd,
                    env=env,
                    bufsize=0,
                )
            except (OSError, ValueError) as exc:
                raise MemOSUnavailableError(f"cannot start MemOS bridge: {exc}") from exc
            self._process = process
            threading.Thread(
                target=self._read_stdout, args=(process,), name="memos-stdout", daemon=True
            ).start()
            threading.Thread(
                target=self._drain_stderr, args=(process,), name="memos-stderr", daemon=True
            ).start()
            return process

    def _read_stdout(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        while True:
            try:
                line = process.stdout.readline(self.config.max_line_bytes + 1)
            except OSError as exc:
                self._fail_all(
                    MemOSTransportError(f"MemOS stdout failed: {exc}", ambiguous=True), process
                )
                return
            if not line:
                self._fail_all(
                    MemOSTransportError("MemOS bridge closed stdout", ambiguous=True), process
                )
                return
            if len(line) > self.config.max_line_bytes or not line.endswith(b"\n"):
                self._fail_all(
                    MemOSProtocolError(
                        "MemOS response exceeds maximum line size", ambiguous=True,
                    ), process
                )
                self._stop_process(process)
                return
            try:
                message = json.loads(line)
                self._validate_message(message)
            except (UnicodeDecodeError, json.JSONDecodeError, MemOSProtocolError) as exc:
                # The reviewed 2.0.10 bridge emits pre-init logger records to
                # stdout before its config can disable the console sink. Keep
                # that narrowly recognized upstream defect out of JSON-RPC
                # framing while rejecting every other malformed line.
                if isinstance(exc, json.JSONDecodeError) and _UPSTREAM_CONSOLE_LOG.match(line):
                    self._remember_stderr(line)
                    continue
                error = MemOSProtocolError(
                    str(exc) if isinstance(exc, MemOSProtocolError)
                    else f"invalid JSON from MemOS bridge: {exc}",
                    ambiguous=True,
                )
                self._fail_all(error, process)
                self._stop_process(process)
                return
            if "id" not in message:  # JSON-RPC notification; intentionally ignored.
                continue
            request_id = message["id"]
            if isinstance(request_id, str):
                self._dispatch_reverse_request(process, message)
                continue
            with self._state_lock:
                target = self._pending.pop(request_id, None)
            # A response can race a timeout. Late responses must not poison the
            # next call, whose monotonically increasing ID is different.
            if target is not None:
                target.put(message)

    @staticmethod
    def _validate_message(message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise MemOSProtocolError("response is not JSON-RPC 2.0")
        if "id" not in message:
            if not isinstance(message.get("method"), str):
                raise MemOSProtocolError("notification has no method")
            return
        request_id = message["id"]
        if isinstance(request_id, str):
            if not _REVERSE_REQUEST_ID.fullmatch(request_id):
                raise MemOSProtocolError("reverse request id is invalid")
            if not isinstance(message.get("method"), str):
                raise MemOSProtocolError("reverse request has no method")
            if "result" in message or "error" in message:
                raise MemOSProtocolError("reverse request cannot contain result or error")
            params = message.get("params")
            if params is not None and not isinstance(params, (dict, list)):
                raise MemOSProtocolError("reverse request params are invalid")
            return
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            raise MemOSProtocolError("response id must be an integer")
        if "method" in message:
            raise MemOSProtocolError("response cannot contain method")
        has_result, has_error = "result" in message, "error" in message
        if has_result == has_error:
            raise MemOSProtocolError("response must contain exactly one of result or error")
        if has_error:
            error = message["error"]
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), int)
                or isinstance(error.get("code"), bool)
                or not isinstance(error.get("message"), str)
            ):
                raise MemOSProtocolError("response error object is invalid")

    def _dispatch_reverse_request(
        self, process: subprocess.Popen[bytes], message: Mapping[str, Any],
    ) -> None:
        """Run a narrowly allowlisted server request without taking the operation lock."""
        request_id = message["id"]
        method = message["method"]
        handler = (self.config.request_handlers or {}).get(method)
        if handler is None:
            self._start_reverse_response(
                process,
                request_id,
                {"error": {"code": -32601, "message": "reverse method is not allowed"}},
            )
            return
        if not self._request_slots.acquire(blocking=False):
            self._start_reverse_response(
                process,
                request_id,
                {"error": {"code": -32003, "message": "reverse handler is busy"}},
            )
            return

        responded = False
        response_lock = threading.Lock()

        def respond_once(body: Mapping[str, Any]) -> None:
            nonlocal responded
            with response_lock:
                if responded:
                    return
                responded = True
            self._send_reverse_response(process, request_id, body)

        def timed_out() -> None:
            respond_once(
                {"error": {"code": -32001, "message": "reverse handler timed out"}}
            )

        timer = threading.Timer(self.config.request_timeout, timed_out)
        timer.daemon = True

        def run() -> None:
            try:
                try:
                    result = handler(message.get("params"))
                    respond_once({"result": result})
                except BaseException:
                    respond_once(
                        {"error": {"code": -32000, "message": "reverse handler failed"}}
                    )
            finally:
                timer.cancel()
                self._request_slots.release()
                with self._state_lock:
                    self._request_threads.discard(threading.current_thread())

        worker = threading.Thread(target=run, name="memos-reverse", daemon=True)
        with self._state_lock:
            self._request_threads.add(worker)
        try:
            timer.start()
            worker.start()
        except RuntimeError:
            timer.cancel()
            self._request_slots.release()
            with self._state_lock:
                self._request_threads.discard(worker)
            self._start_reverse_response(
                process,
                request_id,
                {"error": {"code": -32000, "message": "reverse handler failed"}},
            )

    def _start_reverse_response(
        self, process: subprocess.Popen[bytes], request_id: str, body: Mapping[str, Any],
    ) -> None:
        """Send immediate reverse errors with a strictly bounded worker count."""
        if not self._response_slots.acquire(blocking=False):
            error = MemOSProtocolError(
                "reverse response capacity exceeded", ambiguous=True,
            )
            self._fail_all(error, process)
            self._stop_process(process)
            return

        def run() -> None:
            try:
                self._send_reverse_response(process, request_id, body)
            finally:
                self._response_slots.release()
                with self._state_lock:
                    self._request_threads.discard(threading.current_thread())

        worker = threading.Thread(target=run, name="memos-reverse-response", daemon=True)
        with self._state_lock:
            self._request_threads.add(worker)
        try:
            worker.start()
        except RuntimeError:
            self._response_slots.release()
            with self._state_lock:
                self._request_threads.discard(worker)
            error = MemOSProtocolError(
                "reverse response worker failed", ambiguous=True,
            )
            self._fail_all(error, process)
            self._stop_process(process)

    def _send_reverse_response(
        self, process: subprocess.Popen[bytes], request_id: str, body: Mapping[str, Any],
    ) -> None:
        response = {"jsonrpc": "2.0", "id": request_id, **body}
        try:
            payload = json.dumps(
                response, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError):
            payload = self._reverse_error_payload(
                request_id, -32000, "reverse handler returned invalid JSON",
            )
        if len(payload) > self.config.max_line_bytes:
            payload = self._reverse_error_payload(
                request_id, -32002, "reverse result exceeds maximum line size",
            )
        if len(payload) > self.config.max_line_bytes:
            self._fail_all(
                MemOSProtocolError("reverse error exceeds maximum line size", ambiguous=True),
                process,
            )
            self._stop_process(process)
            return
        try:
            self._write_payload(
                process, payload, time.monotonic() + self.config.request_timeout,
            )
        except MemOSBridgeError as exc:
            self._fail_all(exc, process)

    @staticmethod
    def _reverse_error_payload(request_id: str, code: int, message: str) -> bytes:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8") + b"\n"

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        while True:
            try:
                chunk = process.stderr.read(4096)
            except OSError:
                return
            if not chunk:
                return
            self._remember_stderr(chunk)

    def _remember_stderr(self, chunk: bytes) -> None:
        """Keep bounded diagnostics from stderr and known stdout log defects."""
        if not chunk:
            return
        with self._state_lock:
            self._stderr.append(chunk)
            self._stderr_size += len(chunk)
            while self._stderr and self._stderr_size > self.config.stderr_history_bytes:
                self._stderr_size -= len(self._stderr.popleft())

    def _fail_all(self, error: MemOSBridgeError, process: subprocess.Popen[bytes] | None) -> None:
        with self._state_lock:
            if process is not None and process is not self._process:
                return
            pending = tuple(self._pending.values())
            self._pending.clear()
        for target in pending:
            try:
                target.put_nowait(error)
            except queue.Full:
                pass

    def _restart(self) -> None:
        process = self._process
        if process is not None:
            self._stop_process(process)
        with self._state_lock:
            if self._process is process:
                self._process = None

    def _stop_process(
        self, process: subprocess.Popen[bytes], *, timeout: float | None = None,
    ) -> None:
        acquired = (
            self._stop_lock.acquire() if timeout is None
            else self._stop_lock.acquire(timeout=max(0.0, timeout))
        )
        if not acquired:
            return
        try:
            abort_timeout = min(self.config.shutdown_timeout, 0.05)
            if timeout is not None:
                abort_timeout = min(abort_timeout, max(0.0, timeout))
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=abort_timeout)
                except subprocess.TimeoutExpired:
                    pass
            # Terminating first is intentional: closing a pipe while another
            # thread is blocked in write can wait on the file object's lock.
            self._close_stdin(process)
            self._join_writers(timeout=abort_timeout)
            with self._state_lock:
                if self._process is process:
                    self._process = None
        finally:
            self._stop_lock.release()

    def _join_writers(self, *, timeout: float | None = None) -> None:
        current = threading.current_thread()
        timeout = self.config.shutdown_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        with self._state_lock:
            writers = tuple(self._writer_threads)
        for writer in writers:
            if writer is not current:
                writer.join(timeout=max(0.0, deadline - time.monotonic()))

    @staticmethod
    def _close_stdin(process: subprocess.Popen[bytes]) -> None:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass


__all__ = [
    "PINNED_CAPABILITIES",
    "PINNED_MEMOS_VERSION",
    "BridgeConfig",
    "MemOSBridgeClient",
    "MemOSBridgeError",
    "MemOSUnavailableError",
    "MemOSTimeoutError",
    "MemOSTransportError",
    "MemOSProtocolError",
    "MemOSVersionError",
    "MemOSUpstreamError",
]
