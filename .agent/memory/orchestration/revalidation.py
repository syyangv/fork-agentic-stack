"""Evidence gates, outcome accounting, and append-only stale transitions."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from contextlib import contextmanager
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from render_lessons import (
    append_lesson, append_lesson_updates, load_lessons, render_lessons,
)
from candidate_lock import atomic_write_json, candidate_lifecycle_lock
from ._core import SchemaValidationError, validate_schema

from .providers.crg_evidence import (
    CrgEvidenceError, CrgEvidenceProvider, EvidenceLedger,
    _canonicalize_filesystem_prefix, _recent_ledger_entries,
    _reject_symlink_components,
)
from .identity import derive_project_identity


class EvidenceValidationError(ValueError):
    pass


class RevalidationIndex:
    """Rebuildable local reverse links and stale overrides; never authoritative."""

    def __init__(self, path: str | Path) -> None:
        self.path = _canonicalize_filesystem_prefix(
            Path(path).expanduser().absolute()
        )
        try:
            _reject_symlink_components(self.path.parent)
        except CrgEvidenceError as exc:
            raise EvidenceValidationError(str(exc)) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            _reject_symlink_components(self.path.parent)
            parent = self.path.parent.stat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or (hasattr(os, "getuid") and parent.st_uid != os.getuid())
            ):
                raise EvidenceValidationError("revalidation index parent is unsafe")
            os.chmod(self.path.parent, 0o700)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            try:
                opened = os.fstat(descriptor)
                before = self.path.lstat()
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
                ):
                    raise EvidenceValidationError("revalidation index file is unsafe")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except (CrgEvidenceError, OSError) as exc:
            raise EvidenceValidationError("revalidation index cannot be opened safely") from exc
        with self._connect() as connection:
            connection.executescript("""
                create table if not exists links (
                    target_kind text not null, target_id text not null,
                    provider text not null, provider_id text not null,
                    evidence_id text not null,
                    primary key(target_kind,target_id,provider,provider_id,evidence_id)
                );
                create table if not exists stale_overrides (
                    provider text not null, provider_id text not null,
                    reason text not null, updated_at text not null,
                    primary key(provider,provider_id)
                );
                create table if not exists events (
                    event_id text primary key, target_kind text not null,
                    target_id text not null, reason text not null,
                    observed_at text not null
                );
            """)

    @contextmanager
    def _connect(self, timeout: float = 5):
        timeout = max(0.001, min(float(timeout), 5.0))
        connection = sqlite3.connect(self.path, timeout=timeout)
        try:
            connection.execute(f"pragma busy_timeout={max(1, int(timeout * 1000))}")
            connection.execute("pragma journal_mode=wal")
            with connection:
                yield connection
        finally:
            connection.close()

    def link_candidate(self, candidate: Mapping[str, Any]) -> None:
        provider_ids = candidate.get("provider_ids", {})
        if not isinstance(provider_ids, Mapping):
            return
        evidence_ids = [str(value)[:128] for value in candidate.get("evidence_refs", [])]
        with self._connect() as connection:
            for provider_id in provider_ids.values():
                if not isinstance(provider_id, str) or not provider_id:
                    continue
                for evidence_id in evidence_ids or [""]:
                    connection.execute(
                        "insert or ignore into links values (?,?,?,?,?)",
                        ("candidate", str(candidate.get("id"))[:512], "memos-local",
                        provider_id[:512], evidence_id),
                    )

    def rebuild_from_candidates(self, candidates_dir: str | Path) -> int:
        """Rebuild reverse links from bounded, non-symlink lifecycle files."""
        root = Path(candidates_dir)
        rows: list[tuple[str, str, str]] = []
        for subdir in (Path("."), Path("deferred"), Path("rejected"), Path("graduated")):
            directory = root / subdir
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in sorted(directory.glob("*.json")):
                if len(rows) >= 10_000 or path.is_symlink():
                    break
                try:
                    candidate = __import__("json").loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(candidate, Mapping):
                    continue
                provider_ids = candidate.get("provider_ids", {})
                evidence_ids = candidate.get("evidence_refs", [])
                if not isinstance(provider_ids, Mapping) or not isinstance(evidence_ids, list):
                    continue
                for provider_id in provider_ids.values():
                    if not isinstance(provider_id, str) or not provider_id:
                        continue
                    for evidence_id in evidence_ids or [""]:
                        if isinstance(evidence_id, str):
                            rows.append((str(candidate.get("id"))[:512],
                                         provider_id[:512], evidence_id[:128]))
        with self._connect() as connection:
            connection.execute("delete from links")
            connection.executemany(
                "insert or ignore into links values (?,?,?,?,?)",
                [("candidate", candidate_id, "memos-local", provider_id, evidence_id)
                 for candidate_id, provider_id, evidence_id in rows],
            )
        return len(rows)

    def mark_evidence_stale(
        self, evidence_ids: Sequence[str], reason: str, event_id: str,
    ) -> list[str]:
        affected: list[str] = []
        with self._connect() as connection:
            rows = connection.execute(
                f"select distinct target_kind,target_id,provider,provider_id from links "
                f"where evidence_id in ({','.join('?' for _ in evidence_ids)})",
                tuple(evidence_ids),
            ).fetchall() if evidence_ids else []
            for target_kind, target_id, provider, provider_id in rows:
                was_stale = connection.execute(
                    "select 1 from stale_overrides where provider=? and provider_id=?",
                    (provider, provider_id),
                ).fetchone() is not None
                connection.execute(
                    "insert or ignore into events values (?,?,?,?,?)",
                    (f"{event_id}:{target_kind}:{target_id}", target_kind, target_id,
                     reason[:500], _now()),
                )
                connection.execute(
                    "insert into stale_overrides values (?,?,?,?) "
                    "on conflict(provider,provider_id) do update set reason=excluded.reason,updated_at=excluded.updated_at",
                    (provider, provider_id, reason[:500], _now()),
                )
                if not was_stale:
                    affected.append(str(target_id))
        return affected

    def is_provider_stale(
        self, provider: str, provider_id: str, *, timeout: float = 0.01,
    ) -> bool:
        try:
            with self._connect(timeout) as connection:
                row = connection.execute(
                    "select 1 from stale_overrides where provider=? and provider_id=?",
                    (provider, provider_id),
                ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            raise EvidenceValidationError(
                "revalidation index lookup could not complete within its deadline"
            ) from exc

    def clear_provider_stale(
        self, provider: str, provider_ids: Sequence[str],
    ) -> int:
        values = [value[:512] for value in provider_ids if isinstance(value, str) and value]
        if not values:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                f"delete from stale_overrides where provider=? and provider_id in "
                f"({','.join('?' for _ in values)})",
                (provider, *values),
            )
        return cursor.rowcount


def load_evidence_rows(path: str | Path) -> list[dict[str, Any]]:
    """Use the hardened bounded ledger reader and reject every symlink layer."""
    ledger = _canonicalize_filesystem_prefix(Path(path).expanduser().absolute())
    lock = ledger.with_suffix(ledger.suffix + ".lock")
    try:
        _reject_symlink_components(ledger.parent)
    except CrgEvidenceError as exc:
        raise EvidenceValidationError(str(exc)) from exc
    if ledger.is_symlink() or lock.is_symlink():
        raise EvidenceValidationError("evidence ledger paths must not be symlinks")
    try:
        return _recent_ledger_entries(ledger)
    except (CrgEvidenceError, OSError) as exc:
        raise EvidenceValidationError(str(exc)) from exc


def validate_candidate_evidence(
    candidate: Mapping[str, Any], evidence_rows: Sequence[Mapping[str, Any]],
    *, project_id: str, revision: str, graph_updated_at: str | None,
) -> dict[str, Any]:
    """Require current, project-bound structural and executed-test evidence."""
    if candidate.get("project_scope", {}).get("project_id") != project_id:
        raise EvidenceValidationError("candidate project scope does not match")
    if not candidate.get("code_specific"):
        return {
            "eligible": True, "validated_at": _now(),
            "repository_revision": revision,
            "crg_evidence_ids": [], "test_evidence_ids": [],
        }

    by_id: dict[str, Mapping[str, Any]] = {}
    digests: dict[str, str] = {}
    for row in evidence_rows:
        try:
            validate_schema(row, "evidence-ledger-v1.schema.json")
        except (SchemaValidationError, OSError) as exc:
            raise EvidenceValidationError("evidence row failed schema validation") from exc
        evidence_id = row.get("evidence_id")
        if not isinstance(evidence_id, str):
            continue
        digest = hashlib.sha256(
            __import__("json").dumps(row, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if evidence_id in digests and digests[evidence_id] != digest:
            raise EvidenceValidationError("conflicting duplicate evidence identity")
        digests[evidence_id] = digest
        by_id[evidence_id] = row

    requested = [
        value for value in candidate.get("evidence_refs", [])
        if isinstance(value, str) and value.startswith("evi_")
    ]
    if not requested:
        raise EvidenceValidationError("code-specific candidate has no ledger evidence")
    missing = [value for value in requested if value not in by_id]
    if missing:
        raise EvidenceValidationError("candidate evidence is missing")

    crg_ids: list[str] = []
    test_ids: list[str] = []
    covered_symbols: set[tuple[str, str]] = set()
    for evidence_id in requested:
        row = by_id[evidence_id]
        provenance = row.get("provenance")
        verification = row.get("verification")
        if not isinstance(provenance, Mapping) or not isinstance(verification, Mapping):
            raise EvidenceValidationError("evidence structure is invalid")
        if (
            provenance.get("source_id") != evidence_id
            or provenance.get("project_id") != project_id
            or provenance.get("repository_revision") != revision
            or provenance.get("freshness") != "fresh"
        ):
            raise EvidenceValidationError("evidence is foreign, stale, or unbound")
        kind = provenance.get("kind")
        locator = provenance.get("locator")
        if not isinstance(locator, Mapping):
            raise EvidenceValidationError("evidence locator is invalid")
        if kind in {"crg_node", "crg_flow"}:
            if (
                provenance.get("provider") != "crg"
                or verification.get("repository_reconciled") is not True
                or verification.get("files_reconciled") is not True
                or verification.get("symbols_reconciled") is not True
                or locator.get("graph_updated_at") != graph_updated_at
                or locator.get("working_tree", False) is not False
            ):
                raise EvidenceValidationError("CRG evidence is not currently reconciled")
            for symbol in locator.get("symbols", []):
                if isinstance(symbol, Mapping):
                    covered_symbols.add((
                        str(symbol.get("file_path", "")),
                        str(symbol.get("qualified_name", "")),
                    ))
            crg_ids.append(evidence_id)
        elif kind == "test_run":
            if (
                provenance.get("provider") != "test-runner"
                or verification.get("repository_reconciled") is not True
                or verification.get("executed_test") is not True
                or locator.get("executed_test") is not True
                or locator.get("exit_code") != 0
                or not isinstance(locator.get("test_ids"), list)
                or not locator.get("test_ids")
            ):
                raise EvidenceValidationError("test evidence does not prove a passing run")
            command_digest = locator.get("command_digest")
            if command_digest is not None and not _sha256(command_digest):
                raise EvidenceValidationError("test command digest is invalid")
            test_ids.append(evidence_id)
        else:
            raise EvidenceValidationError("unsupported evidence kind")

    if not crg_ids or not test_ids:
        raise EvidenceValidationError("code-specific graduation needs CRG and test evidence")
    declared = {
        (str(row.get("file_path", "")), str(row.get("qualified_name", "")))
        for row in candidate.get("code_refs", []) if isinstance(row, Mapping)
    }
    if not declared:
        raise EvidenceValidationError("code-specific candidate has no declared code refs")
    if declared and not declared.issubset(covered_symbols):
        raise EvidenceValidationError("CRG evidence does not cover candidate code refs")
    return {
        "eligible": True, "validated_at": _now(),
        "repository_revision": revision,
        "crg_evidence_ids": crg_ids, "test_evidence_ids": test_ids,
    }


def validate_live_candidate_evidence(
    candidate: Mapping[str, Any], agent_root: str | Path, *,
    repo_root: str | Path | None = None, registry_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve the current graph, ledger, revision, file hashes, and dirty state."""
    agent = Path(agent_root).resolve(strict=False)
    repo = Path(repo_root or os.environ.get("AGENTIC_PROJECT_ROOT", agent.parent)).resolve(
        strict=False
    )
    identity = derive_project_identity(repo, os.environ.get("AGENTIC_GIT_REMOTE"))
    ledger_file = Path(ledger_path or agent / "memory/evidence/ledger.jsonl")
    provider = CrgEvidenceProvider(
        repo_root=repo, project_id=identity.project_id,
        registry_path=registry_path or os.environ.get("AGENTIC_CRG_REGISTRY"),
        ledger=EvidenceLedger(ledger_file),
    )
    health = provider.health()
    if health.get("status") != "healthy":
        raise EvidenceValidationError("CRG graph is unavailable or stale")
    revision = health.get("repository_revision")
    if not isinstance(revision, str) or not revision:
        raise EvidenceValidationError("repository revision is unavailable")
    rows = load_evidence_rows(ledger_file)
    report = validate_candidate_evidence(
        candidate, rows, project_id=identity.project_id, revision=revision,
        graph_updated_at=health.get("graph_updated_at"),
    )
    by_id = {row.get("evidence_id"): row for row in rows}
    linked_paths: list[str] = []
    for evidence_id in report["crg_evidence_ids"]:
        locator = by_id[evidence_id]["provenance"]["locator"]
        symbols = locator.get("symbols", [])
        try:
            provider._validate_symbols(symbols, Path(health["database"]))
        except (CrgEvidenceError, OSError) as exc:
            raise EvidenceValidationError("linked CRG symbols are stale") from exc
        for symbol in symbols:
            if not isinstance(symbol, Mapping):
                continue
            relative = str(symbol.get("file_path", ""))
            _reject_linked_symlink(repo, relative)
            if relative and relative not in linked_paths:
                linked_paths.append(relative)
    if linked_paths:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *linked_paths],
            cwd=repo, capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode != 0 or result.stdout.strip():
            raise EvidenceValidationError("linked code has uncommitted changes")
    return report


def apply_outcome(
    record: Mapping[str, Any], outcome: str, outcome_id: str,
) -> dict[str, Any]:
    """Idempotently update observational counts without changing trust status."""
    if outcome not in {"used", "contradicted", "ignored"}:
        raise ValueError("unsupported candidate outcome")
    if not isinstance(outcome_id, str) or not outcome_id or len(outcome_id) > 512:
        raise ValueError("outcome identity must contain 1 to 512 characters")
    updated = dict(record)
    history = [dict(row) for row in record.get("outcome_history", [])
               if isinstance(row, Mapping)]
    prior = next((row for row in history if row.get("outcome_id") == outcome_id), None)
    if prior:
        if prior.get("outcome") != outcome:
            raise ValueError("outcome identity was reused with different content")
        return updated
    if len(history) >= 1000:
        # Preserve replay safety rather than evicting old identities and later
        # double-counting them. New observations are ignored at the bound.
        return updated
    history.append({
        "outcome_id": str(outcome_id)[:512], "outcome": outcome,
        "observed_at": _now(),
    })
    updated["outcome_history"] = history
    updated["support_count"] = int(record.get("support_count", 0)) + int(outcome == "used")
    updated["contradiction_count"] = (
        int(record.get("contradiction_count", 0)) + int(outcome == "contradicted")
    )
    return updated


def revalidate_lessons(
    semantic_dir: str | Path, *, project_id: str, revision: str,
    evidence_rows: Sequence[Mapping[str, Any]], graph_updated_at: str | None,
    live_validator: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> list[str]:
    """Append stale tombstones; never delete or automatically re-accept."""
    semantic = Path(semantic_dir)
    def build_updates(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        latest: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            if isinstance(row.get("id"), str):
                latest[str(row["id"])] = row
        updates: list[dict[str, Any]] = []
        for lesson_id, lesson in latest.items():
            if lesson.get("status") != "accepted" or not lesson.get("code_specific"):
                continue
            snapshot = lesson.get("evidence_snapshot")
            stale_reason = None
            if not isinstance(snapshot, Mapping) or snapshot.get("repository_revision") != revision:
                stale_reason = "repository_revision_changed"
            else:
                candidate_view = {
                    "project_scope": lesson.get("project_scope", {}),
                    "code_specific": True,
                    "evidence_refs": lesson.get("evidence_ids", []),
                    "code_refs": lesson.get("code_refs", []),
                }
                try:
                    if live_validator is not None:
                        live_validator(candidate_view)
                    else:
                        validate_candidate_evidence(
                            candidate_view, evidence_rows, project_id=project_id,
                            revision=revision, graph_updated_at=graph_updated_at,
                        )
                except EvidenceValidationError as exc:
                    stale_reason = str(exc)
            if stale_reason:
                updates.append({
                    **lesson, "status": "revalidation_needed",
                    "revalidation_requested_at": _now(),
                    "revalidation_reason": stale_reason,
                })
        return updates

    updates = append_lesson_updates(str(semantic), build_updates)
    changed = [str(row["id"]) for row in updates]
    if updates:
        render_lessons(str(semantic))
    return changed


def record_retrieval_outcome(agent_root: str | Path, event: Any) -> list[str]:
    """Map only memos:* observations to candidates/lessons, idempotently."""
    if getattr(event, "event_type", None) != "retrieval.used":
        return []
    payload = event.payload
    outcome = str(payload.get("outcome", "used"))
    if outcome not in {"used", "contradicted", "ignored"}:
        return []
    raw_ids = payload.get("item_ids", [])
    provider_ids = {
        value.split(":", 1)[1] for value in raw_ids
        if isinstance(value, str) and value.startswith("memos:") and ":" in value
    } if isinstance(raw_ids, (list, tuple)) else set()
    if not provider_ids:
        return []
    root = Path(agent_root)
    candidates = root / "memory/candidates"
    updated_ids: list[str] = []
    with candidate_lifecycle_lock(str(candidates)):
        for subdir in (Path("."), Path("graduated")):
            directory = candidates / subdir
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                try:
                    candidate = __import__("json").loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                ids = candidate.get("provider_ids", {})
                if not isinstance(ids, Mapping) or not provider_ids.intersection(ids.values()):
                    continue
                revised = apply_outcome(candidate, outcome, event.event_id)
                if revised != candidate:
                    atomic_write_json(str(path), revised)
                    updated_ids.append(str(candidate.get("id")))
    if updated_ids:
        semantic = root / "memory/semantic"
        def build_updates(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            latest: dict[str, Mapping[str, Any]] = {}
            for lesson in rows:
                source = lesson.get("source_candidate")
                if source in updated_ids:
                    latest[str(source)] = lesson
            updates = []
            for lesson in latest.values():
                if lesson.get("status") not in {"accepted", "provisional"}:
                    continue
                revised = apply_outcome(lesson, outcome, event.event_id)
                if revised != lesson:
                    updates.append(revised)
            return updates

        updates = append_lesson_updates(str(semantic), build_updates)
        if updates:
            render_lessons(str(semantic))
    return updated_ids


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    suffix = value[7:]
    return len(suffix) == 64 and all(char in "0123456789abcdef" for char in suffix)


def _reject_linked_symlink(repo: Path, relative: str) -> None:
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise EvidenceValidationError("linked source path is unsafe")
    current = repo
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceValidationError("linked source path contains a symlink")
