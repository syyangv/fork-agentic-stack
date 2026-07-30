"""Private, bounded scheduler run state with no prompt or candidate bodies."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Mapping


SCHEMA = "agentic.scheduler-health.v1"
LABELS = frozenset({"com.syang.agentic-stack.auto-dream", "com.syang.agentic-stack.review-notify"})
MAX_BYTES = 4096
MAX_RUNNING_AGE = dt.timedelta(hours=1)
_KEYS = frozenset({"schema", "label", "status", "started_at", "completed_at", "duration_ms",
                   "tool_version", "source_revision", "candidate_count", "rejection_count",
                   "notification", "run_token"})
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_directory(fd: int) -> bool:
    info = os.fstat(fd)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022


def _open_directory_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    descriptor = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor); descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor); raise


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("scheduler health write made no progress")
        view = view[count:]


def _bounded_text(value: object, name: str, *, pattern: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(name + " is invalid")
    return value


def _parse_stamp(value: object) -> dt.datetime:
    text = _bounded_text(value, "timestamp", pattern=r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
    try:
        return dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc


class SchedulerHealthStore:
    """Per-job atomic state under an explicit owner-controlled agent root."""
    def __init__(self, agent_root: str | Path) -> None:
        self.root = Path(agent_root)
        if not self.root.is_absolute() or "\x00" in str(self.root):
            raise ValueError("scheduler health root is invalid")

    def start(self, label: str, *, tool_version: str, source_revision: str) -> dict[str, object]:
        token = uuid.uuid4().hex
        state = {
            "schema": SCHEMA, "label": label, "status": "running", "started_at": _stamp(),
            "completed_at": None, "duration_ms": None,
            "tool_version": _bounded_text(tool_version, "tool version", pattern=r"[A-Za-z0-9._-]{1,64}"),
            "source_revision": _bounded_text(source_revision, "source revision", pattern=r"(?:unknown|[0-9a-f]{7,64})"),
            "candidate_count": None, "rejection_count": None, "notification": "not_requested",
            "run_token": token,
        }
        self._validate_label(label)
        self._validate_state(state, label)
        root_fd, directory = self._directory(); lock = self._lock(directory)
        try:
            previous = self._read_locked(directory, label)
            if previous is not None and previous.get("status") == "running":
                age = dt.datetime.now(dt.timezone.utc) - _parse_stamp(previous.get("started_at"))
                if age < dt.timedelta(0) or age <= MAX_RUNNING_AGE:
                    raise ValueError("scheduler health already has an active run")
            self._write_locked(directory, label, state)
            return dict(state)
        finally:
            os.close(lock); os.close(directory); os.close(root_fd)

    def finish(self, label: str, *, run_token: str, success: bool, duration_ms: int,
               candidate_count: int | None = None, rejection_count: int | None = None,
               notification: str = "not_requested") -> dict[str, object]:
        _bounded_text(run_token, "run token", pattern=r"[0-9a-f]{32}")
        if type(success) is not bool:
            raise ValueError("success is invalid")
        for name, value in (("duration", duration_ms), ("candidate count", candidate_count), ("rejection count", rejection_count)):
            if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000):
                raise ValueError(name + " is invalid")
        if notification not in {"not_requested", "deferred", "sent", "failed"}:
            raise ValueError("notification outcome is invalid")
        root_fd, directory = self._directory(); lock = self._lock(directory)
        try:
            previous = self._read_locked(directory, label)
            if (previous is None or previous.get("status") != "running"
                    or previous.get("run_token") != run_token):
                raise ValueError("scheduler health run token does not own the active run")
            state = {
                **previous, "status": "success" if success else "failure", "completed_at": _stamp(),
                "duration_ms": duration_ms, "candidate_count": candidate_count,
                "rejection_count": rejection_count, "notification": notification,
            }
            self._validate_state(state, label)
            self._write_locked(directory, label, state)
            return dict(state)
        finally:
            os.close(lock); os.close(directory); os.close(root_fd)

    def _directory(self) -> tuple[int, int]:
        root_fd = _open_directory_nofollow(self.root)
        if not _safe_directory(root_fd):
            os.close(root_fd); raise ValueError("scheduler health root is not owner-safe")
        runtime_fd = health_fd = -1
        try:
            for parent, name in ((root_fd, "runtime"),):
                try:
                    runtime_fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    os.mkdir(name, 0o700, dir_fd=parent); runtime_fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
                if not _safe_directory(runtime_fd):
                    raise ValueError("scheduler health directory is not owner-safe")
            try:
                health_fd = os.open("scheduler-health", _DIR_FLAGS, dir_fd=runtime_fd)
            except FileNotFoundError:
                os.mkdir("scheduler-health", 0o700, dir_fd=runtime_fd)
                health_fd = os.open("scheduler-health", _DIR_FLAGS, dir_fd=runtime_fd)
            if not _safe_directory(health_fd):
                raise ValueError("scheduler health directory is not owner-safe")
            return root_fd, health_fd
        except BaseException:
            if health_fd >= 0: os.close(health_fd)
            os.close(root_fd)
            raise
        finally:
            if runtime_fd >= 0:
                os.close(runtime_fd)

    def _lock(self, directory: int) -> int:
        fd = os.open(".scheduler-health.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd); raise ValueError("scheduler health lock is not owner-safe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _read(self, label: str) -> dict[str, object] | None:
        self._validate_label(label)
        root_fd, directory = self._directory(); lock = self._lock(directory)
        try:
            return self._read_locked(directory, label)
        finally:
            os.close(lock); os.close(directory); os.close(root_fd)

    def _read_locked(self, directory: int, label: str) -> dict[str, object] | None:
        name = label + ".json"
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("scheduler health state is not owner-safe")
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        try:
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                    or opened.st_mode & 0o077):
                raise ValueError("scheduler health state identity changed")
            raw = os.read(fd, MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_BYTES:
            raise ValueError("scheduler health state exceeds bound")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("scheduler health state is invalid") from exc
        self._validate_state(value, label)
        return value

    def _write(self, label: str, state: Mapping[str, object]) -> dict[str, object]:
        self._validate_label(label)
        self._validate_state(state, label)
        root_fd, directory = self._directory(); lock = self._lock(directory)
        try:
            self._read_locked(directory, label)  # reject a symlink/special replacement before replace.
            self._write_locked(directory, label, state)
            return dict(state)
        finally:
            os.close(lock); os.close(directory); os.close(root_fd)

    def _write_locked(self, directory: int, label: str, state: Mapping[str, object]) -> None:
        raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("scheduler health state exceeds bound")
        temporary = "." + label + "." + uuid.uuid4().hex + ".tmp"
        fd = -1
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
            os.fchmod(fd, 0o600)
            _write_all(fd, raw)
            os.fsync(fd)
            os.close(fd); fd = -1
            os.replace(temporary, label + ".json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    @staticmethod
    def _validate_state(state: object, label: str) -> None:
        if not isinstance(state, Mapping) or set(state) != _KEYS:
            raise ValueError("scheduler health state is invalid")
        if state.get("schema") != SCHEMA or state.get("label") != label:
            raise ValueError("scheduler health state is invalid")
        if state.get("status") not in {"running", "success", "failure"}:
            raise ValueError("scheduler health state is invalid")
        started = _parse_stamp(state.get("started_at"))
        _bounded_text(state.get("tool_version"), "tool version", pattern=r"[A-Za-z0-9._-]{1,64}")
        _bounded_text(state.get("source_revision"), "source revision",
                      pattern=r"(?:unknown|[0-9a-f]{7,64})")
        _bounded_text(state.get("run_token"), "run token", pattern=r"[0-9a-f]{32}")
        if state.get("notification") not in {"not_requested", "deferred", "sent", "failed"}:
            raise ValueError("scheduler health state is invalid")
        for key in ("duration_ms", "candidate_count", "rejection_count"):
            value = state.get(key)
            if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000):
                raise ValueError("scheduler health state is invalid")
        running = state.get("status") == "running"
        if running != (state.get("completed_at") is None and state.get("duration_ms") is None):
            raise ValueError("scheduler health state is invalid")
        if running and any(state.get(key) is not None for key in ("candidate_count", "rejection_count")):
            raise ValueError("scheduler health state is invalid")
        if not running:
            completed = _parse_stamp(state.get("completed_at"))
            if completed < started:
                raise ValueError("scheduler health state is invalid")

    @staticmethod
    def _validate_label(label: str) -> None:
        if label not in LABELS:
            raise ValueError("scheduler health label is invalid")
