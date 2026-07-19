"""Persistent idempotent delivery journal for the MemOS shadow provider."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


_LOCAL_WORKER_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_WORKER_LOCKS_GUARD = threading.Lock()
_WORKER_LOCAL = threading.local()


def stable_project_lock_path(project_root: str | Path) -> str:
    """Return an owner-only lifecycle lock outside the swappable project tree."""
    root = Path(project_root)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.parent / f".{root.name}.memos-lifecycle.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (
            hasattr(os, "getuid") and info.st_uid != os.getuid()
        ):
            raise OSError("MemOS lifecycle lock is not an owner regular file")
        os.fchmod(descriptor, 0o600)
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    return str(lock)


@contextmanager
def _project_lock(lock_path: str, *, timeout: float | None = None):
    """Acquire the reentrant per-project OS lock before touching runtime state."""
    with _LOCAL_WORKER_LOCKS_GUARD:
        local_lock = _LOCAL_WORKER_LOCKS.setdefault(lock_path, threading.RLock())
    deadline = time.monotonic() + timeout if timeout is not None else None
    acquired = local_lock.acquire() if timeout is None else local_lock.acquire(timeout=timeout)
    if not acquired:
        raise TimeoutError("timed out acquiring MemOS project lock")
    try:
        held = getattr(_WORKER_LOCAL, "held", None)
        if held is None:
            held = {}
            _WORKER_LOCAL.held = held
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
                if timeout is None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                else:
                    assert deadline is not None
                    while True:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("timed out acquiring MemOS project lock")
                            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            elif msvcrt is not None:  # pragma: no cover - Windows
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if timeout is None:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    assert deadline is not None
                    while True:
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("timed out acquiring MemOS project lock")
                            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
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
    finally:
        local_lock.release()


class JournalConflict(ValueError):
    """A stable delivery key was reused with different content."""


@dataclass(frozen=True, slots=True)
class Delivery:
    sequence: int
    delivery_id: str
    event_id: str
    idempotency_key: str
    method: str
    params: dict
    retryable: bool
    attempts: int


class MemosDeliveryJournal:
    def __init__(
        self, path: str | Path, *, max_attempts: int = 3,
        initialize_timeout: float | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_attempts = max_attempts
        if max_attempts <= 0:
            raise ValueError("maximum attempts must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._worker_lock_path = stable_project_lock_path(self.path.parent)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.initialization_error: str | None = None
        try:
            deadline = (
                time.monotonic() + initialize_timeout
                if initialize_timeout is not None else None
            )
            with _project_lock(
                self._worker_lock_path, timeout=initialize_timeout,
            ):
                self._initialize(timeout=(
                    10 if deadline is None else max(0.0, deadline - time.monotonic())
                ))
        except TimeoutError:
            if initialize_timeout is None:
                raise
            self.initialization_error = "memos_journal_initialization_timeout"

    def _connect(self, *, timeout: float = 10) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute(f"pragma busy_timeout={max(0, int(timeout * 1000))}")
        connection.execute("pragma journal_mode=wal")
        return connection

    @contextmanager
    def _connection(self, *, timeout: float = 10):
        connection = self._connect(timeout=timeout)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _immediate_connection(self, *, timeout: float = 10):
        connection = self._connect(timeout=timeout)
        try:
            connection.execute("begin immediate")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self, *, timeout: float = 10) -> None:
        with self._connection(timeout=timeout) as connection:
            connection.executescript(
                """
                create table if not exists deliveries (
                    sequence integer primary key autoincrement,
                    delivery_id text not null unique,
                    event_id text not null,
                    idempotency_key text not null,
                    method text not null,
                    params_json text not null,
                    payload_hash text not null,
                    state text not null check(state in
                        ('pending','inflight','delivered','ambiguous','dead')),
                    retryable integer not null check(retryable in (0,1)),
                    attempts integer not null default 0,
                    last_error text,
                    created_at text not null,
                    updated_at text not null,
                    unique(idempotency_key, method)
                );
                create index if not exists deliveries_state_sequence
                    on deliveries(state, sequence);
                create table if not exists tool_events (
                    event_id text primary key,
                    idempotency_key text not null unique,
                    run_id text not null,
                    tool_json text not null,
                    payload_hash text not null,
                    created_at text not null
                );
                create index if not exists tool_events_run
                    on tool_events(run_id, created_at, event_id);
                create table if not exists run_lifecycle (
                    run_id text primary key,
                    session_id text not null,
                    episode_id text,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists deferred_completions (
                    run_id text primary key,
                    event_id text not null unique,
                    idempotency_key text not null unique,
                    event_json text not null,
                    payload_hash text not null,
                    state text not null check(state in ('pending','materialized')),
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists retrieval_observations (
                    run_id text not null,
                    item_id text not null,
                    reason text not null,
                    outcome text not null check(outcome in
                        ('selected','used','contradicted','ignored')),
                    created_at text not null,
                    updated_at text not null,
                    primary key(run_id,item_id,reason)
                );
                create index if not exists retrieval_observations_run
                    on retrieval_observations(run_id, created_at, item_id);
                create table if not exists retrieval_invocations (
                    run_id text not null,
                    reason text not null,
                    created_at text not null,
                    primary key(run_id,reason)
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def delivery_worker(self, *, timeout: float | None = None):
        """Serialize FIFO RPC delivery from claim through terminal transition."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        with _project_lock(self._worker_lock_path, timeout=timeout):
            if self.initialization_error:
                self._initialize(timeout=(
                    10 if deadline is None else max(0.0, deadline - time.monotonic())
                ))
                self.initialization_error = None
            self._recover_orphaned_inflight(timeout=(
                10 if deadline is None else max(0.0, deadline - time.monotonic())
            ))
            yield

    def _recover_orphaned_inflight(self, *, timeout: float = 10) -> None:
        # Holding the worker lock proves no live compliant worker owns these
        # rows; OS locks are released automatically when a process crashes.
        with self._immediate_connection(timeout=timeout) as connection:
            connection.execute(
                "update deliveries set state=case "
                "when retryable=0 then 'ambiguous' "
                "when attempts<? then 'pending' else 'dead' end, "
                "last_error='recovered after interrupted delivery',updated_at=? "
                "where state='inflight'",
                (self.max_attempts, _now()),
            )

    def enqueue(
        self, event_id: str, idempotency_key: str, method: str,
        params: dict, retryable: bool,
    ) -> bool:
        encoded = _canonical(params)
        payload_hash = _digest(encoded)
        delivery_id = _digest(f"{idempotency_key}\0{method}")
        now = _now()
        with self._immediate_connection() as connection:
            return self._enqueue_locked(
                connection, delivery_id, event_id, idempotency_key, method,
                encoded, payload_hash, retryable, now,
            )

    @staticmethod
    def _enqueue_locked(
        connection: sqlite3.Connection, delivery_id: str, event_id: str,
        idempotency_key: str, method: str, encoded: str, payload_hash: str,
        retryable: bool, now: str,
    ) -> bool:
        existing = connection.execute(
            "select payload_hash from deliveries where idempotency_key=? and method=?",
            (idempotency_key, method),
        ).fetchone()
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise JournalConflict(
                    f"delivery key {idempotency_key!r}/{method!r} changed content"
                )
            return False
        connection.execute(
            """insert into deliveries
               (delivery_id,event_id,idempotency_key,method,params_json,payload_hash,
                state,retryable,attempts,created_at,updated_at)
               values (?,?,?,?,?,?,'pending',?,0,?,?)""",
            (delivery_id, event_id, idempotency_key, method, encoded,
             payload_hash, int(retryable), now, now),
        )
        return True

    def claim_next(self) -> Delivery | None:
        connection = self._connect()
        try:
            connection.execute("begin immediate")
            row = connection.execute(
                "select * from deliveries where state='pending' order by sequence limit 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "update deliveries set state='inflight', attempts=attempts+1, updated_at=? "
                "where delivery_id=?", (_now(), row["delivery_id"]),
            )
            connection.commit()
            return Delivery(
                sequence=row["sequence"], delivery_id=row["delivery_id"],
                event_id=row["event_id"], idempotency_key=row["idempotency_key"],
                method=row["method"], params=json.loads(row["params_json"]),
                retryable=bool(row["retryable"]), attempts=row["attempts"] + 1,
            )
        finally:
            connection.close()

    def mark_delivered(self, delivery_id: str) -> None:
        self._set_terminal(delivery_id, "delivered", None)

    def mark_failed(
        self, delivery_id: str, error: str, *, ambiguous: bool,
        retryable_failure: bool = True,
    ) -> str:
        with self._immediate_connection() as connection:
            row = connection.execute(
                "select retryable,attempts from deliveries where delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            if ambiguous and not row["retryable"]:
                state = "ambiguous"
            elif (
                retryable_failure and row["attempts"] < self.max_attempts
            ):
                state = "pending"
            else:
                state = "dead"
            connection.execute(
                "update deliveries set state=?, last_error=?, updated_at=? where delivery_id=?",
                (state, error[:1000], _now(), delivery_id),
            )
        return state

    def _set_terminal(self, delivery_id: str, state: str, error: str | None) -> None:
        with self._immediate_connection() as connection:
            cursor = connection.execute(
                "update deliveries set state=?, last_error=?, updated_at=? where delivery_id=?",
                (state, error, _now(), delivery_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(delivery_id)

    def store_tool(
        self, event_id: str, idempotency_key: str, run_id: str, tool: dict,
    ) -> bool:
        encoded = _canonical(tool)
        payload_hash = _digest(encoded)
        with self._immediate_connection() as connection:
            row = connection.execute(
                "select event_id,idempotency_key,payload_hash from tool_events "
                "where event_id=? or idempotency_key=?",
                (event_id, idempotency_key),
            ).fetchone()
            if row:
                if (
                    row["event_id"] != event_id
                    or row["idempotency_key"] != idempotency_key
                    or row["payload_hash"] != payload_hash
                ):
                    raise JournalConflict(f"tool event key {idempotency_key!r} changed content")
                return False
            connection.execute(
                "insert into tool_events values (?,?,?,?,?,?)",
                (event_id, idempotency_key, run_id, encoded, payload_hash, _now()),
            )
            return True

    def record_retrievals(
        self, run_id: str, item_ids: list[str], reason: str,
    ) -> int:
        now = _now()
        inserted = 0
        with self._immediate_connection() as connection:
            for item_id in dict.fromkeys(item_ids[:100]):
                cursor = connection.execute(
                    "insert or ignore into retrieval_observations "
                    "(run_id,item_id,reason,outcome,created_at,updated_at) "
                    "values (?,?,?,'selected',?,?)",
                    (run_id[:512], item_id[:512], reason[:100], now, now),
                )
                inserted += cursor.rowcount
        return inserted

    def record_retrieval_invocation(
        self, run_id: str, reason: str, *, timeout: float = 10,
    ) -> bool:
        with self._immediate_connection(timeout=timeout) as connection:
            cursor = connection.execute(
                "insert or ignore into retrieval_invocations values (?,?,?)",
                (run_id[:512], reason[:100], _now()),
            )
            return cursor.rowcount == 1

    def mark_retrievals(
        self, run_id: str, item_ids: list[str], outcome: str, *, reason: str,
    ) -> int:
        if outcome not in {"used", "contradicted", "ignored"}:
            raise ValueError("retrieval outcome must be used, contradicted, or ignored")
        updated = 0
        with self._immediate_connection() as connection:
            for item_id in dict.fromkeys(item_ids[:100]):
                cursor = connection.execute(
                    "update retrieval_observations set outcome=?,updated_at=? "
                    "where run_id=? and item_id=? and reason=?",
                    (outcome, _now(), run_id[:512], item_id[:512], reason[:100]),
                )
                updated += cursor.rowcount
        return updated

    def finalize_retrievals(self, run_id: str) -> int:
        with self._immediate_connection() as connection:
            cursor = connection.execute(
                "update retrieval_observations set outcome='ignored',updated_at=? "
                "where run_id=? and outcome='selected'",
                (_now(), run_id[:512]),
            )
            return cursor.rowcount

    def retrievals_for_run(self, run_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "select item_id,reason,outcome from retrieval_observations "
                "where run_id=? order by created_at,item_id",
                (run_id[:512],),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_retrieval_reason(self, run_id: str, reason: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "select 1 from retrieval_invocations where run_id=? and reason=? limit 1",
                (run_id[:512], reason[:100]),
            ).fetchone()
        return row is not None

    def retrieval_reasons_for_run(self, run_id: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "select reason from retrieval_invocations where run_id=? "
                "order by created_at,reason",
                (run_id[:512],),
            ).fetchall()
        return [str(row["reason"]) for row in rows]

    def tools_for_run(self, run_id: str, *, limit: int = 100) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "select tool_json from tool_events where run_id=? "
                "order by created_at,event_id limit ?", (run_id, limit),
            ).fetchall()
        return [json.loads(row["tool_json"]) for row in rows]

    def begin_run(self, run_id: str, session_id: str) -> None:
        now = _now()
        with self._immediate_connection() as connection:
            row = connection.execute(
                "select session_id from run_lifecycle where run_id=?", (run_id,),
            ).fetchone()
            if row and row["session_id"] != session_id:
                raise JournalConflict(f"run {run_id!r} changed session identity")
            connection.execute(
                "insert or ignore into run_lifecycle values (?,?,null,?,?)",
                (run_id, session_id, now, now),
            )

    def set_episode(self, run_id: str, episode_id: str) -> None:
        with self._immediate_connection() as connection:
            row = connection.execute(
                "select episode_id from run_lifecycle where run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["episode_id"] not in (None, episode_id):
                raise JournalConflict(f"run {run_id!r} changed MemOS episode identity")
            connection.execute(
                "update run_lifecycle set episode_id=?,updated_at=? where run_id=?",
                (episode_id, _now(), run_id),
            )

    def lifecycle(self, run_id: str) -> dict[str, str | None] | None:
        with self._connection() as connection:
            row = connection.execute(
                "select session_id,episode_id from run_lifecycle where run_id=?", (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def episode_ids(self, *, limit: int = 100) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "select episode_id from run_lifecycle where episode_id is not null "
                "order by updated_at desc limit ?", (max(1, min(limit, 100)),),
            ).fetchall()
        return [row["episode_id"] for row in rows]

    def defer_completion(
        self, run_id: str, event_id: str, idempotency_key: str, event: dict,
    ) -> bool:
        encoded = _canonical(event)
        payload_hash = _digest(encoded)
        now = _now()
        with self._immediate_connection() as connection:
            row = connection.execute(
                "select run_id,event_id,idempotency_key,payload_hash "
                "from deferred_completions where run_id=? or event_id=? "
                "or idempotency_key=?",
                (run_id, event_id, idempotency_key),
            ).fetchone()
            if row:
                if (
                    row["run_id"] != run_id
                    or row["event_id"] != event_id
                    or row["idempotency_key"] != idempotency_key
                    or row["payload_hash"] != payload_hash
                ):
                    raise JournalConflict(f"run {run_id!r} changed deferred completion")
                return False
            connection.execute(
                "insert into deferred_completions values (?,?,?,?,?,'pending',?,?)",
                (run_id, event_id, idempotency_key, encoded, payload_hash, now, now),
            )
        return True

    def deferred_completion(self, run_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "select event_json from deferred_completions "
                "where run_id=? and state='pending'", (run_id,),
            ).fetchone()
        return json.loads(row["event_json"]) if row else None

    def materialize_completion(
        self, run_id: str, event_id: str, idempotency_key: str,
        deliveries: list[tuple[str, dict, bool]],
    ) -> int:
        """Atomically publish all deferred completion RPCs or none of them."""
        now = _now()
        inserted = 0
        with self._immediate_connection() as connection:
            row = connection.execute(
                "select state,event_id,idempotency_key from deferred_completions "
                "where run_id=?", (run_id,),
            ).fetchone()
            if row is None or row["state"] == "materialized":
                return 0
            if (
                row["event_id"] != event_id
                or row["idempotency_key"] != idempotency_key
            ):
                raise JournalConflict(f"run {run_id!r} changed completion identity")
            for method, params, retryable in deliveries:
                encoded = _canonical(params)
                payload_hash = _digest(encoded)
                delivery_id = _digest(f"{idempotency_key}\0{method}")
                inserted += int(self._enqueue_locked(
                    connection, delivery_id, event_id, idempotency_key, method,
                    encoded, payload_hash, retryable, now,
                ))
            connection.execute(
                "update deferred_completions set state='materialized',updated_at=? "
                "where run_id=?", (now, run_id),
            )
        return inserted

    def counts(self) -> dict[str, int]:
        values = {name: 0 for name in ("pending", "inflight", "delivered", "ambiguous", "dead")}
        with self._connection() as connection:
            for row in connection.execute("select state,count(*) count from deliveries group by state"):
                values[row["state"]] = row["count"]
            values["deferred"] = connection.execute(
                "select count(*) from deferred_completions where state='pending'"
            ).fetchone()[0]
        return values


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
