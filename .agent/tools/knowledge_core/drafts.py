"""Private, rebuildable storage for unapproved knowledge drafts.

This module intentionally has no vault writer.  A draft is a record that a
later approval/apply phase may inspect; it is not a Markdown write request
that this phase can execute.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1
P6_REVIEW_STATUSES = frozenset({"draft", "accepted", "rejected"})
_PROTECTED_RUNTIME = Path("~/.agent/knowledge").expanduser().resolve()


def _is_protected_runtime_path(path: str | Path) -> bool:
    resolved = Path(path).expanduser().resolve()
    return resolved == _PROTECTED_RUNTIME or _PROTECTED_RUNTIME in resolved.parents


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class DraftStore:
    """SQLite store for draft metadata, content, and capture diagnostics.

    The path is explicit so callers cannot accidentally initialize the normal
    ``~/.agent/knowledge`` runtime while testing P5.  ``:memory:`` is useful
    for unit tests and uses one connection because SQLite memory databases are
    connection-local.
    """

    def __init__(self, path: str | Path, *, allow_protected_runtime: bool = False):
        self.path = str(path)
        self._lock = RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
            self._initialize(self._memory_connection)
            return

        resolved = Path(path).expanduser().resolve()
        protected_runtime_path = _is_protected_runtime_path(resolved)
        if protected_runtime_path and not allow_protected_runtime:
            raise ValueError("P5 draft storage must not initialize ~/.agent/knowledge")
        if allow_protected_runtime and not protected_runtime_path:
            raise ValueError("allow_protected_runtime requires a path inside ~/.agent/knowledge")
        resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = str(resolved)
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            self._initialize(connection)
        finally:
            connection.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # The database remains usable on platforms without chmod support.
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._memory_connection is not None:
                yield self._memory_connection
                return
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version not in (0, SCHEMA_VERSION):
            raise RuntimeError(f"Unsupported knowledge draft store schema: {current_version}")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                candidate_hash TEXT NOT NULL,
                target_path TEXT NOT NULL,
                target_action TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                validation_evidence_json TEXT NOT NULL,
                validation_level TEXT NOT NULL,
                uncertainties_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, turn_id, candidate_hash)
            );

            CREATE INDEX IF NOT EXISTS drafts_task_turn_idx
                ON drafts(task_id, turn_id);

            CREATE TABLE IF NOT EXISTS captures (
                capture_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                candidate_hash TEXT,
                status TEXT NOT NULL,
                diagnostics_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                personal_context_used INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, turn_id, candidate_hash, status)
            );

            CREATE INDEX IF NOT EXISTS captures_task_turn_idx
                ON captures(task_id, turn_id);

            -- P6 metadata is additive so a P5 draft database remains readable
            -- without a destructive schema migration. Audit rows contain
            -- bounded decisions and hashes, never the session transcript.
            CREATE TABLE IF NOT EXISTS draft_metadata (
                draft_id TEXT PRIMARY KEY,
                target_base_hash TEXT,
                FOREIGN KEY(draft_id) REFERENCES drafts(draft_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS draft_reviews (
                review_id TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                draft_hash TEXT NOT NULL,
                target_hash TEXT,
                desired_hash TEXT,
                preimage_hash TEXT,
                post_write_hash TEXT,
                approved_by TEXT,
                reason TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(draft_id) REFERENCES drafts(draft_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS draft_reviews_draft_idx
                ON draft_reviews(draft_id, created_at, review_id);
            """
        )
        if current_version == 0:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()

    @staticmethod
    def _capture_id(task_id: str, turn_id: str, candidate_hash: str | None, status: str) -> str:
        key = "\0".join((task_id, turn_id, candidate_hash or "", status))
        return "capture_" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    def put_draft(self, draft: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert a draft once and return ``(stored_draft, was_created)``."""

        required = (
            "draft_id",
            "task_id",
            "turn_id",
            "candidate_hash",
            "target_path",
            "target_action",
            "title",
            "content",
            "source_refs",
            "validation_evidence",
            "validation_level",
            "uncertainties",
        )
        missing = [key for key in required if key not in draft]
        if missing:
            raise ValueError("draft is missing required fields")
        created_at = str(draft.get("created_at") or _now_iso())
        updated_at = str(draft.get("updated_at") or created_at)
        status = str(draft.get("status") or "draft")
        row_values = (
            str(draft["draft_id"]),
            str(draft["task_id"]),
            str(draft["turn_id"]),
            str(draft["candidate_hash"]),
            str(draft["target_path"]),
            str(draft["target_action"]),
            str(draft["title"]),
            str(draft["content"]),
            _json(draft["source_refs"]),
            _json(draft["validation_evidence"]),
            str(draft["validation_level"]),
            _json(draft["uncertainties"]),
            status,
            created_at,
            updated_at,
        )
        with self._connection() as connection:
            insert = connection.execute(
                """
                INSERT OR IGNORE INTO drafts (
                    draft_id, task_id, turn_id, candidate_hash, target_path,
                    target_action, title, content, source_refs_json,
                    validation_evidence_json, validation_level, uncertainties_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_values,
            )
            row = connection.execute(
                """
                SELECT d.*, m.target_base_hash
                FROM drafts AS d
                LEFT JOIN draft_metadata AS m ON m.draft_id = d.draft_id
                WHERE d.task_id = ? AND d.turn_id = ? AND d.candidate_hash = ?
                """,
                (str(draft["task_id"]), str(draft["turn_id"]), str(draft["candidate_hash"])),
            ).fetchone()
            if row is None:
                raise RuntimeError("Draft insert did not produce a readable record")
            was_created = insert.rowcount == 1
            if "target_base_hash" in draft:
                connection.execute(
                    """
                    INSERT INTO draft_metadata (draft_id, target_base_hash)
                    VALUES (?, ?)
                    ON CONFLICT(draft_id) DO UPDATE SET target_base_hash = excluded.target_base_hash
                    """,
                    (str(draft["draft_id"]), draft.get("target_base_hash")),
                )
                row = connection.execute(
                    """
                    SELECT d.*, m.target_base_hash
                    FROM drafts AS d
                    LEFT JOIN draft_metadata AS m ON m.draft_id = d.draft_id
                    WHERE d.task_id = ? AND d.turn_id = ? AND d.candidate_hash = ?
                    """,
                    (str(draft["task_id"]), str(draft["turn_id"]), str(draft["candidate_hash"])),
                ).fetchone()
            return self._draft_from_row(row), was_created

    def record_capture(
        self,
        *,
        task_id: str,
        turn_id: str,
        candidate_hash: str | None,
        status: str,
        diagnostics: list[Mapping[str, Any]],
        provenance: list[Mapping[str, Any]],
        personal_context_used: bool,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Retain bounded status/diagnostics without retaining the raw answer."""

        timestamp = created_at or _now_iso()
        capture_id = self._capture_id(task_id, turn_id, candidate_hash, status)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO captures (
                    capture_id, task_id, turn_id, candidate_hash, status,
                    diagnostics_json, provenance_json, personal_context_used, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    task_id,
                    turn_id,
                    candidate_hash,
                    status,
                    _json(list(diagnostics)),
                    _json(list(provenance)),
                    1 if personal_context_used else 0,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Capture insert did not produce a readable record")
            return self._capture_from_row(row)

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT d.*, m.target_base_hash
                FROM drafts AS d
                LEFT JOIN draft_metadata AS m ON m.draft_id = d.draft_id
                WHERE d.draft_id = ?
                """,
                (draft_id,),
            ).fetchone()
            return self._draft_from_row(row) if row is not None else None

    def list_drafts(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if task_id is None:
                rows = connection.execute(
                    """
                    SELECT d.*, m.target_base_hash
                    FROM drafts AS d
                    LEFT JOIN draft_metadata AS m ON m.draft_id = d.draft_id
                    ORDER BY d.created_at, d.draft_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT d.*, m.target_base_hash
                    FROM drafts AS d
                    LEFT JOIN draft_metadata AS m ON m.draft_id = d.draft_id
                    WHERE d.task_id = ? ORDER BY d.created_at, d.draft_id
                    """, (task_id,)
                ).fetchall()
            return [self._draft_from_row(row) for row in rows]

    def bind_target_base_hash(
        self,
        draft_id: str,
        target_base_hash: str | None,
        *,
        expected_status: str = "draft",
    ) -> dict[str, Any]:
        """Bind an update preimage without changing P5 draft content."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown draft: {draft_id}")
            if row["status"] != expected_status:
                raise ValueError(f"Draft is not in {expected_status} state")
            connection.execute(
                """
                INSERT INTO draft_metadata (draft_id, target_base_hash)
                VALUES (?, ?)
                ON CONFLICT(draft_id) DO UPDATE SET target_base_hash = excluded.target_base_hash
                """,
                (draft_id, target_base_hash),
            )
            joined = connection.execute(
                """
                SELECT d.*, m.target_base_hash
                FROM drafts AS d
                LEFT JOIN draft_metadata AS m ON m.draft_id = d.draft_id
                WHERE d.draft_id = ?
                """,
                (draft_id,),
            ).fetchone()
            if joined is None:
                raise RuntimeError("Draft metadata update did not produce a readable record")
            return self._draft_from_row(joined)

    def update_draft_status(
        self,
        draft_id: str,
        status: str,
        *,
        expected_status: str = "draft",
        review: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically change status and append bounded review/audit metadata."""

        if status not in {"accepted", "rejected"}:
            raise ValueError("P6 only commits accepted or rejected draft states")
        timestamp = _now_iso()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE drafts
                SET status = ?, updated_at = ?
                WHERE draft_id = ? AND status = ?
                """,
                (status, timestamp, draft_id, expected_status),
            ).rowcount
            if updated != 1:
                row = connection.execute(
                    "SELECT status FROM drafts WHERE draft_id = ?", (draft_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown draft: {draft_id}")
                if row["status"] != status:
                    raise ValueError(
                        f"Draft status transition refused: {row['status']} -> {status}"
                    )
            if review is not None:
                required = ("review_id", "decision", "draft_hash", "state")
                if any(key not in review for key in required):
                    raise ValueError("Review audit is missing required fields")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO draft_reviews (
                        review_id, draft_id, decision, draft_hash, target_hash,
                        desired_hash, preimage_hash, post_write_hash, approved_by,
                        reason, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(review["review_id"]),
                        draft_id,
                        str(review["decision"]),
                        str(review["draft_hash"]),
                        review.get("target_hash"),
                        review.get("desired_hash"),
                        review.get("preimage_hash"),
                        review.get("post_write_hash"),
                        review.get("approved_by"),
                        review.get("reason"),
                        str(review["state"]),
                        str(review.get("created_at") or timestamp),
                    ),
                )
            row = connection.execute(
                """
                SELECT d.*, m.target_base_hash
                FROM drafts AS d
                LEFT JOIN draft_metadata AS m ON m.draft_id = d.draft_id
                WHERE d.draft_id = ?
                """,
                (draft_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Draft status update did not produce a readable record")
            return self._draft_from_row(row)

    def list_reviews(self, *, draft_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if draft_id is None:
                rows = connection.execute(
                    "SELECT * FROM draft_reviews ORDER BY created_at, review_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM draft_reviews
                    WHERE draft_id = ? ORDER BY created_at, review_id
                    """,
                    (draft_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def list_captures(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if task_id is None:
                rows = connection.execute("SELECT * FROM captures ORDER BY created_at, capture_id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM captures WHERE task_id = ? ORDER BY created_at, capture_id", (task_id,)
                ).fetchall()
            return [self._capture_from_row(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._memory_connection is not None:
                self._memory_connection.close()
                self._memory_connection = None

    def __enter__(self) -> DraftStore:
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "draft_id": row["draft_id"],
            "task_id": row["task_id"],
            "turn_id": row["turn_id"],
            "candidate_hash": row["candidate_hash"],
            "target_path": row["target_path"],
            "target_action": row["target_action"],
            "target_base_hash": row["target_base_hash"] if "target_base_hash" in row.keys() else None,
            "draft_hash": hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
            "title": row["title"],
            "content": row["content"],
            "source_refs": _loads(row["source_refs_json"], []),
            "validation_level": row["validation_level"],
            "validation_evidence": _loads(row["validation_evidence_json"], []),
            "uncertainties": _loads(row["uncertainties_json"], []),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _capture_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "capture_id": row["capture_id"],
            "task_id": row["task_id"],
            "turn_id": row["turn_id"],
            "candidate_hash": row["candidate_hash"],
            "status": row["status"],
            "diagnostics": _loads(row["diagnostics_json"], []),
            "provenance": _loads(row["provenance_json"], []),
            "personal_context_used": bool(row["personal_context_used"]),
            "created_at": row["created_at"],
        }


__all__ = ["DraftStore", "SCHEMA_VERSION"]
