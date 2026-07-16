"""Cross-process serialization for candidate lifecycle mutations."""
from __future__ import annotations

import contextlib
import os
import tempfile
import threading

try:  # POSIX
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


_LOCAL = threading.local()


def _held() -> dict[str, tuple[object, int]]:
    locks = getattr(_LOCAL, "locks", None)
    if locks is None:
        locks = {}
        _LOCAL.locks = locks
    return locks


@contextlib.contextmanager
def candidate_lifecycle_lock(candidates_dir: str):
    """Serialize lifecycle read-modify-write windows, reentrantly per thread."""
    os.makedirs(candidates_dir, exist_ok=True)
    lock_path = os.path.join(candidates_dir, ".lifecycle.lock")
    held = _held()
    if lock_path in held:
        handle, depth = held[lock_path]
        held[lock_path] = (handle, depth + 1)
        try:
            yield
        finally:
            handle, depth = held[lock_path]
            held[lock_path] = (handle, depth - 1)
        return

    handle = open(lock_path, "a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        held[lock_path] = (handle, 1)
        yield
    finally:
        held.pop(lock_path, None)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def atomic_write_json(path: str, value: object) -> None:
    """Replace a JSON file atomically so readers never observe a partial row."""
    import json

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".candidate-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
