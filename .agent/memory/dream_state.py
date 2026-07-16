"""Structured, atomic health state for the staging-only dream cycle."""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import time
import uuid
from pathlib import Path


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"schema_version": 1}


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dream-state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def start_cycle(path: str | Path) -> str:
    path = Path(path)
    state = _load(path)
    run_id = uuid.uuid4().hex
    state.update(
        schema_version=1,
        current_run_id=run_id,
        last_started_at=_utc_now(),
        last_status="running",
        last_error=None,
    )
    state.setdefault("last_success_at", None)
    state.setdefault("last_failure_at", None)
    _write(path, state)
    return run_id


def finish_cycle(
    path: str | Path,
    run_id: str,
    *,
    started_monotonic: float,
    now_monotonic: float | None = None,
) -> None:
    state = _load(Path(path))
    if state.get("current_run_id") != run_id:
        return
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    state.update(
        last_status="success",
        last_success_at=_utc_now(),
        last_completed_at=_utc_now(),
        last_duration_ms=round((now_monotonic - started_monotonic) * 1000),
        last_error=None,
    )
    _write(Path(path), state)


def fail_cycle(
    path: str | Path,
    run_id: str,
    error: BaseException,
    *,
    started_monotonic: float,
    now_monotonic: float | None = None,
) -> None:
    state = _load(Path(path))
    if state.get("current_run_id") != run_id:
        return
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    state.update(
        last_status="failure",
        last_failure_at=_utc_now(),
        last_completed_at=_utc_now(),
        last_duration_ms=round((now_monotonic - started_monotonic) * 1000),
        last_error=f"{type(error).__name__}: {error}"[:1000],
    )
    _write(Path(path), state)
