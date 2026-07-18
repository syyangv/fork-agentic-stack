#!/usr/bin/env python3
"""Normalize native harness hooks into bounded memory EventEnvelopes.

This is the only adapter boundary allowed to construct behavioral events.
It deliberately keeps raw prompts, environments, and full tool payloads out
of MemOS, and it treats delivery failure as observable degradation rather
than a reason to fail the host harness.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


HERE = Path(__file__).resolve().parent
AGENT_ROOT = HERE.parents[1]
sys.path.insert(0, str(AGENT_ROOT / "memory"))

from orchestration._core import canonical_json, redact  # noqa: E402
from orchestration.contracts import EventEnvelope  # noqa: E402
from orchestration.identity import derive_project_identity  # noqa: E402


SIGNALS = (
    "user_prompt", "pre_tool", "post_tool", "feedback",
    "subagent_start", "finalize",
)

# A false value is intentional: instruction-only adapters must not imply that
# they observed lifecycle events. Unsupported task start and feedback can be
# submitted only through this module's explicit CLI surface.
CAPABILITIES: dict[str, dict[str, bool]] = {
    "antigravity": dict.fromkeys(SIGNALS, False),
    "claude-code": {
        "user_prompt": True, "pre_tool": True, "post_tool": True,
        "feedback": False, "subagent_start": True, "finalize": True,
    },
    "codex": dict.fromkeys(SIGNALS, False),
    "copilot-cli": {
        "user_prompt": True, "pre_tool": True, "post_tool": True,
        "feedback": False, "subagent_start": False, "finalize": True,
    },
    "cursor": dict.fromkeys(SIGNALS, False),
    "gemini": {
        "user_prompt": True, "pre_tool": True, "post_tool": True,
        "feedback": False, "subagent_start": False, "finalize": True,
    },
    "hermes": dict.fromkeys(SIGNALS, False),
    "openclaw": dict.fromkeys(SIGNALS, False),
    "opencode": dict.fromkeys(SIGNALS, False),
    "pi": {
        "user_prompt": True, "pre_tool": False, "post_tool": True,
        "feedback": False, "subagent_start": False, "finalize": True,
    },
    "standalone-python": dict.fromkeys(SIGNALS, False),
    "windsurf": dict.fromkeys(SIGNALS, False),
}


class HookEventError(ValueError):
    pass


class NoActiveRun(HookEventError):
    pass


class AlreadyActiveRun(HookEventError):
    pass


@dataclass(frozen=True, slots=True)
class Correlation:
    run_id: str
    session_id: str
    start_event_id: str
    intent: str
    finalizing: bool = False


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    status: str
    reason: str


def _ensure_private_dir(path: Path) -> None:
    """Create or repair a runtime directory so only its owner can traverse it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _ensure_private_tree(path: Path) -> None:
    # Runtime artifacts live below runtime/orchestration/<store>. Protect the
    # whole subtree, not only the leaf directory, even when it already exists.
    for directory in reversed((path, path.parent, path.parent.parent)):
        _ensure_private_dir(directory)


def _atomic_private_write(path: Path, value: str) -> None:
    temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _exclusive_file_lock(path: Path, *, timeout: float | None = None):
    """Take a cross-process lock, optionally bounding critical-path waits."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    stream = os.fdopen(descriptor, "a+")
    deadline = None if timeout is None else time.monotonic() + timeout
    acquired = False
    unlock: Callable[[], None] | None = None
    try:
        try:
            import fcntl
            try:
                if timeout is None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    acquired = True
                else:
                    while not acquired:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                        except BlockingIOError:
                            if deadline is not None and time.monotonic() >= deadline:
                                break
                            time.sleep(0.01)
                if acquired:
                    unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                acquired = False
        except ImportError:
            try:
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write("\0")
                    stream.flush()
                stream.seek(0)
                while not acquired:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                    except OSError:
                        if deadline is not None and time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)

                if acquired:
                    def unlock() -> None:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                acquired = False
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            try:
                if unlock is not None:
                    unlock()
            except OSError:
                pass
    finally:
        stream.close()


class CorrelationStore:
    """Small per-session state files; no prompts or tool payloads are stored."""

    def __init__(self, agent_root: str | Path = AGENT_ROOT) -> None:
        self.root = Path(agent_root) / "runtime" / "orchestration" / "correlation"

    def _path(self, harness: str, session_id: str) -> Path:
        digest = hashlib.sha256(f"{harness}\0{session_id}".encode()).hexdigest()
        return self.root / f"{digest}.json"

    def _lock_path(self, harness: str, session_id: str) -> Path:
        return self._path(harness, session_id).with_suffix(".lock")

    def _read(self, harness: str, session_id: str) -> Correlation | None:
        path = self._path(harness, session_id)
        try:
            path.chmod(0o600)
            value = json.loads(path.read_text(encoding="utf-8"))
            return Correlation(**value)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return None

    def set(self, harness: str, correlation: Correlation) -> None:
        _ensure_private_tree(self.root)
        path = self._path(harness, correlation.session_id)
        with _exclusive_file_lock(
            self._lock_path(harness, correlation.session_id), timeout=0.25,
        ) as acquired:
            if not acquired:
                raise OSError("unable to lock correlation state")
            _atomic_private_write(path, json.dumps({
                "run_id": correlation.run_id,
                "session_id": correlation.session_id,
                "start_event_id": correlation.start_event_id,
                "intent": correlation.intent[:2000],
                "finalizing": correlation.finalizing,
            }, sort_keys=True))

    def current(self, harness: str, session_id: str) -> Correlation | None:
        _ensure_private_tree(self.root)
        return self._read(harness, session_id)

    def clear(self, harness: str, session_id: str) -> None:
        _ensure_private_tree(self.root)
        with _exclusive_file_lock(self._lock_path(harness, session_id), timeout=0.25) as acquired:
            if not acquired:
                raise OSError("unable to lock correlation state")
            try:
                self._path(harness, session_id).unlink()
            except FileNotFoundError:
                pass

    def clear_if_run(self, harness: str, session_id: str, run_id: str) -> bool:
        """Clear only when the file still belongs to the finalizing run."""
        _ensure_private_tree(self.root)
        with _exclusive_file_lock(self._lock_path(harness, session_id), timeout=0.25) as acquired:
            if not acquired:
                raise OSError("unable to lock correlation state")
            current = self._read(harness, session_id)
            if current is None or current.run_id != run_id:
                return False
            try:
                self._path(harness, session_id).unlink()
            except FileNotFoundError:
                return False
            return True


class HookEventSpool:
    """Durable handoff between latency-sensitive hooks and the MemOS worker."""

    def __init__(self, agent_root: str | Path = AGENT_ROOT) -> None:
        self.root = Path(agent_root) / "runtime" / "orchestration" / "hook-events"
        self.pending_dir = self.root / "pending"
        self.delivered_dir = self.root / "delivered"
        self.health_file = self.root / "health.json"
        self.lock_file = self.root / "worker.lock"

    def enqueue(self, event: EventEnvelope) -> Path:
        _ensure_private_tree(self.root)
        _ensure_private_dir(self.pending_dir)
        stamp = "".join(ch for ch in event.timestamp if ch.isdigit())[:20]
        path = self.pending_dir / f"{stamp}-{event.event_id}.json"
        encoded = event.canonical_json()
        if path.exists():
            path.chmod(0o600)
            if path.read_text(encoding="utf-8") != encoded:
                raise HookEventError("event spool conflict for stable event ID")
            return path
        _atomic_private_write(path, encoded)
        return path

    def pending(self, limit: int = 100) -> list[Path]:
        _ensure_private_tree(self.root)
        if not self.pending_dir.is_dir():
            return []
        _ensure_private_dir(self.pending_dir)
        paths = sorted(self.pending_dir.glob("*.json"))[:limit]
        for path in paths:
            path.chmod(0o600)
        return paths

    def mark_delivered(self, paths: list[Path]) -> None:
        _ensure_private_tree(self.root)
        _ensure_private_dir(self.delivered_dir)
        for path in paths:
            if path.exists():
                path.chmod(0o600)
                destination = self.delivered_dir / path.name
                os.replace(path, destination)
                destination.chmod(0o600)

    def write_health(self, status: str, reason: str, pending: int) -> None:
        _ensure_private_tree(self.root)
        value = {
            "schema": "agentic.memory.hook-delivery-health.v1",
            "status": status,
            "reason": reason[:500],
            "pending": pending,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        _atomic_private_write(self.health_file, json.dumps(value, sort_keys=True))

    @contextlib.contextmanager
    def worker_lock(self):
        _ensure_private_tree(self.root)
        with _exclusive_file_lock(self.lock_file) as acquired:
            yield acquired


def normalize_event(
    harness: str,
    signal: str,
    payload: Mapping[str, Any],
    *,
    repo_root: str | Path,
    agent_root: str | Path = AGENT_ROOT,
    timestamp: str | None = None,
    store: CorrelationStore | None = None,
    explicit: bool = False,
) -> EventEnvelope:
    if harness not in CAPABILITIES:
        raise HookEventError(f"unknown harness: {harness}")
    if signal not in SIGNALS:
        raise HookEventError(f"unknown hook signal: {signal}")
    if not isinstance(payload, Mapping):
        raise HookEventError("hook payload must be a JSON object")
    if not CAPABILITIES[harness][signal] and not (
        explicit and signal in {"user_prompt", "feedback", "finalize"}
    ):
        raise HookEventError(f"{harness} does not natively provide {signal}")

    root = Path(repo_root).expanduser().resolve(strict=False)
    session_id = _session_id(payload)
    store = store or CorrelationStore(agent_root)
    timestamp = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    identity = derive_project_identity(root, _git(root, "config", "--get", "remote.origin.url"))
    revision = _git(root, "rev-parse", "HEAD") or None

    correlation = store.current(harness, session_id)
    actor = "system"
    code_refs: tuple[dict[str, Any], ...] = ()

    if signal == "user_prompt":
        if correlation is not None and not correlation.finalizing:
            raise AlreadyActiveRun(f"run already active for {harness} session {session_id}")
        if correlation is not None:
            store.clear(harness, session_id)
        intent = _prompt_intent(payload)
        if not intent:
            raise HookEventError("user prompt event has no prompt text")
        run_seed = _first(payload, "prompt_id", "promptId", "event_id", "eventId") or timestamp
        run_id = "run_" + hashlib.sha256(
            f"{harness}\0{session_id}\0{run_seed}".encode()
        ).hexdigest()[:24]
        event_type = "task.started"
        actor = "user"
        normalized = {"source_signal": signal}
        parents: tuple[str, ...] = ()
    else:
        if correlation is None:
            raise NoActiveRun(f"no active run for {harness} session {session_id}")
        run_id = correlation.run_id
        intent = correlation.intent
        parents = (correlation.start_event_id,)
        if signal == "pre_tool":
            event_type, actor = "tool.started", "agent"
            normalized = _tool_payload(payload, include_output=False)
        elif signal == "post_tool":
            event_type, actor = "tool.completed", "tool"
            normalized = _tool_payload(payload, include_output=True)
        elif signal == "feedback":
            event_type, actor = "feedback.recorded", "user"
            normalized = _feedback_payload(payload)
        elif signal == "subagent_start":
            event_type, actor = "subagent.started", "agent"
            normalized = {
                "agent_type": _bounded(_first(payload, "agent_type", "agentType", "subagent_type") or "subagent", 100),
                "description": _bounded(_first(payload, "description", "task") or "", 500),
            }
        else:
            event_type, actor = "task.completed", "agent"
            normalized = {
                "status": _bounded(_first(payload, "status", "reason", "stop_reason") or "completed", 100),
                "source_signal": signal,
            }

    source_id = _first(
        payload, "tool_use_id", "toolUseId", "tool_call_id", "toolCallId",
        "event_id", "eventId", "hook_event_name", "hookEventName",
    ) or hashlib.sha256(canonical_json(redact(normalized)).encode()).hexdigest()[:24]
    event = EventEnvelope.create(
        idempotency_key=f"{harness}:{run_id}:{signal}:{source_id}",
        timestamp=timestamp,
        event_type=event_type,
        project_id=identity.project_id,
        repo_root=identity.repo_root,
        revision=revision,
        harness=harness,
        run_id=run_id,
        session_id=session_id,
        actor=actor,
        intent=intent,
        payload=normalized,
        code_refs=code_refs,
        parent_event_ids=parents,
    )
    if signal == "user_prompt":
        store.set(harness, Correlation(run_id, session_id, event.event_id, event.intent))
    elif signal == "finalize":
        store.set(harness, Correlation(
            correlation.run_id, correlation.session_id,
            correlation.start_event_id, correlation.intent, finalizing=True,
        ))
    return event


def deliver_with_timeout(
    event: Mapping[str, Any],
    deliverer: Callable[[Mapping[str, Any], float], Mapping[str, Any]],
    *,
    timeout: float,
) -> CaptureStatus:
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put((True, deliverer(event, timeout)), block=False)
        except Exception as exc:  # delivery is advisory during shadow rollout
            result_queue.put((False, exc), block=False)

    threading.Thread(target=run, daemon=True, name="memory-event-delivery").start()
    try:
        ok, value = result_queue.get(timeout=timeout)
    except queue.Empty:
        return CaptureStatus("degraded", "delivery_timeout")
    if not ok:
        return CaptureStatus("degraded", f"delivery_error:{type(value).__name__}")
    if not isinstance(value, Mapping):
        return CaptureStatus("degraded", "delivery_invalid_result")
    health = value.get("health")
    if isinstance(health, Mapping) and health.get("status") == "degraded":
        warnings = health.get("warnings")
        reason = warnings[0] if isinstance(warnings, list) and warnings else "provider_health"
        return CaptureStatus("degraded", f"provider:{reason}")
    if value.get("status") in {"recorded", "disabled"}:
        return CaptureStatus("captured", str(value.get("status")))
    return CaptureStatus("degraded", "delivery_unconfirmed")


def capture_hook_event(
    harness: str,
    signal: str,
    payload: Mapping[str, Any],
    *,
    timeout: float = 3.0,
    explicit: bool = False,
    repo_root: str | Path | None = None,
    store: CorrelationStore | None = None,
    deliverer: Callable[[Mapping[str, Any], float], Mapping[str, Any]] | None = None,
    spool: HookEventSpool | None = None,
    worker_starter: Callable[[Path], None] | None = None,
) -> tuple[EventEnvelope | None, CaptureStatus]:
    root = Path(repo_root or os.environ.get("AGENTIC_PROJECT_ROOT") or payload.get("cwd") or AGENT_ROOT.parent)
    store = store or CorrelationStore()
    try:
        event = normalize_event(
            harness, signal, payload, repo_root=root, explicit=explicit, store=store,
        )
    except NoActiveRun:
        return None, CaptureStatus("skipped", "no_active_run")
    except AlreadyActiveRun:
        return None, CaptureStatus("skipped", "active_run_exists")
    except (HookEventError, OSError, ValueError):
        return None, CaptureStatus("degraded", "normalization_error")
    if deliverer is None:
        spool = spool or HookEventSpool()
        spool.enqueue(event)
        (worker_starter or _start_spool_worker)(root)
        status = CaptureStatus("captured", "queued")
    else:
        status = deliver_with_timeout(event.to_dict(), deliverer, timeout=timeout)
    if signal == "finalize" and (
        status.status == "captured" or status.reason.startswith("provider:")
    ):
        store.clear_if_run(harness, event.session_id, event.run_id)
    return event, status


def _start_spool_worker(repo_root: Path) -> None:
    env = _worker_environment(repo_root)
    kwargs = {"start_new_session": True}
    if os.name == "nt":
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        cwd=repo_root, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, **kwargs,
    )


def _worker_environment(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["AGENTIC_PROJECT_ROOT"] = str(repo_root)
    remote = _git(repo_root, "config", "--get", "remote.origin.url")
    if remote:
        env["AGENTIC_GIT_REMOTE"] = remote
    else:
        env.pop("AGENTIC_GIT_REMOTE", None)
    return env


def drain_spool(
    spool: HookEventSpool,
    deliverer: Callable[[list[Mapping[str, Any]], float], Mapping[str, Any]],
    *,
    timeout: float = 180.0,
) -> int:
    delivered = 0
    last_health = ("healthy", "idle")
    with spool.worker_lock() as acquired:
        if not acquired:
            return 0
        while True:
            paths = spool.pending()
            if not paths:
                # Give hooks that spawned while this worker held the lock a
                # short handoff window; their own worker exits on lock busy.
                time.sleep(0.15)
                paths = spool.pending()
                if not paths:
                    spool.write_health(*last_health, pending=0)
                    return delivered
            try:
                events = [
                    EventEnvelope.from_external(json.loads(path.read_text(encoding="utf-8"))).to_dict()
                    for path in paths
                ]
                result = deliverer(events, timeout)
                if not isinstance(result, Mapping) or result.get("status") not in {"recorded", "disabled"}:
                    raise RuntimeError("memory recorder did not accept the event batch")
                spool.mark_delivered(paths)
                delivered += len(paths)
                health = result.get("health")
                if isinstance(health, Mapping) and health.get("status") == "degraded":
                    warnings = health.get("warnings")
                    reason = warnings[0] if isinstance(warnings, list) and warnings else "provider_health"
                    last_health = ("degraded", str(reason))
                    spool.write_health(*last_health, pending=len(spool.pending()))
                else:
                    last_health = ("healthy", "delivered")
                    spool.write_health(*last_health, pending=len(spool.pending()))
            except Exception as exc:
                spool.write_health(
                    "degraded", f"{type(exc).__name__}: {exc}", len(spool.pending()),
                )
                return delivered


def _batch_subprocess_deliver(
    events: list[Mapping[str, Any]], timeout: float,
) -> Mapping[str, Any]:
    command = [sys.executable, str(AGENT_ROOT / "tools" / "memory_orchestrate.py"), "record"]
    completed = subprocess.run(
        command, input=json.dumps(events), text=True, capture_output=True,
        timeout=timeout, env=os.environ.copy(),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[:500])
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("memory record returned a non-object")
    return value


def _session_id(payload: Mapping[str, Any]) -> str:
    value = _first(payload, "session_id", "sessionId", "conversation_id", "conversationId")
    if not value:
        raise HookEventError("hook payload has no session identifier")
    return _bounded(value, 512)


def _prompt_intent(payload: Mapping[str, Any]) -> str:
    # Native hooks do not have a trusted local summarizer. Persist a
    # content-free marker rather than leaking or heuristically transforming
    # the full prompt. Semantic intent can enter through an explicit policy in
    # a later contract version.
    value = _first(payload, "prompt", "user_prompt", "input")
    return "user request received" if str(value or "").strip() else ""


def _tool_payload(payload: Mapping[str, Any], *, include_output: bool) -> dict[str, Any]:
    name = _first(payload, "tool_name", "toolName", "name") or "Unknown"
    tool_input = _first_value(payload, "tool_input", "toolInput", "tool_args", "toolArgs", "input")
    result = {
        "tool_name": _bounded(name, 200),
        "input_summary": _summarize_input(tool_input),
    }
    if include_output:
        response = _first_value(payload, "tool_response", "toolResponse", "tool_result", "toolResult", "result")
        result["output_summary"] = _summarize_output(response)
        error = _error_code(response)
        if error:
            result["error_code"] = error
    return result


def _feedback_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    polarity = str(_first(payload, "polarity", "sentiment") or "neutral").lower()
    if polarity not in {"positive", "negative", "neutral"}:
        polarity = "neutral"
    magnitude = _first_value(payload, "magnitude", "score")
    if not isinstance(magnitude, (int, float)) or isinstance(magnitude, bool):
        magnitude = 0.0
    return {
        "polarity": polarity,
        "magnitude": max(0.0, min(1.0, abs(float(magnitude)))),
        "channel": "explicit",
        "rationale": _bounded(_first(payload, "rationale", "message") or "explicit user feedback", 1000),
    }


def _summarize_input(value: Any) -> str:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            value = decoded
        except json.JSONDecodeError:
            return _bounded(redact(value), 1000)
    if isinstance(value, Mapping):
        for key in ("command", "file_path", "path", "description"):
            if isinstance(value.get(key), str):
                return _bounded(redact(value[key]), 1000)
        safe = {key: item for key, item in value.items() if key not in {
            "content", "old_string", "new_string", "oldText", "newText",
            "prompt", "raw_prompt", "environment", "env",
        }}
        return _bounded(canonical_json(redact(safe)), 1000)
    return _bounded(redact(str(value or "")), 1000)


def _summarize_output(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("error", "stderr", "output", "stdout", "textResultForLlm", "result"):
            item = value.get(key)
            if isinstance(item, str) and item:
                return _bounded(redact(item), 1000)
        return ""
    return _bounded(redact(str(value or "")), 1000)


def _error_code(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("is_error") or value.get("isError"):
        return "tool_error"
    exit_code = value.get("exit_code", value.get("exitCode"))
    if isinstance(exit_code, int) and exit_code != 0:
        return f"exit_{exit_code}"
    result_type = value.get("resultType")
    if result_type in {"failure", "denied"}:
        return str(result_type)
    return None


def _bounded(value: Any, limit: int) -> str:
    return str(value)[:limit]


def _first(payload: Mapping[str, Any], *names: str) -> str | None:
    value = _first_value(payload, *names)
    return value if isinstance(value, str) and value else None


def _first_value(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize one harness lifecycle event")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--harness", choices=sorted(CAPABILITIES))
    parser.add_argument("--signal", choices=SIGNALS)
    parser.add_argument("--explicit", action="store_true", help="allow explicit task start/feedback for hookless harnesses")
    parser.add_argument("--no-deliver", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--emit-metadata", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    try:
        if args.worker:
            drain_spool(HookEventSpool(), _batch_subprocess_deliver)
            return 0
        if not args.harness or not args.signal:
            raise HookEventError("--harness and --signal are required")
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise HookEventError("hook payload exceeds 1 MiB")
        payload = json.loads(raw.decode("utf-8") or "{}")
        if args.no_deliver:
            root = Path(os.environ.get("AGENTIC_PROJECT_ROOT", AGENT_ROOT.parent))
            try:
                normalize_event(
                    args.harness, args.signal, payload, repo_root=root,
                    explicit=args.explicit,
                )
            except NoActiveRun:
                pass
        else:
            event, status = capture_hook_event(
                args.harness, args.signal, payload,
                timeout=max(0.1, min(args.timeout, 10.0)), explicit=args.explicit,
            )
            if args.emit_metadata and event is not None:
                print(json.dumps({
                    "event_id": event.event_id,
                    "run_id": event.run_id,
                    "status": status.status,
                    "reason": status.reason,
                }, separators=(",", ":")))
        return 0
    except (HookEventError, json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
        print(f"orchestration event skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
