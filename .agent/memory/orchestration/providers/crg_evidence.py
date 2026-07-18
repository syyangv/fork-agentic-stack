"""Tool-mediated CRG evidence validation and bounded provenance ledger."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._core import (
    SchemaValidationError,
    canonical_json,
    contains_sensitive_plaintext,
    redact,
    validate_schema,
)
from ..contracts import ProvenanceRef


SAFE_TOOLS = {
    "semantic_search": "semantic_search_nodes",
    "graph_query": "query_graph",
    "impact": "get_impact_radius",
    "architecture": "get_architecture_overview",
    "change_review": "detect_changes",
}
CRG_TOOL_NAMES = frozenset(SAFE_TOOLS.values())
GRAPH_QUERY_PATTERNS = frozenset({
    "callers_of", "callees_of", "imports_of", "importers_of", "children_of",
    "tests_for", "inheritors_of", "file_summary",
})
VOLATILE_ROOTS = (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"))
MAX_RECORD_BYTES = 16 * 1024
MAX_SUMMARY_CHARS = 2_000
MAX_SYMBOLS = 50
LOCK_TIMEOUT_SECONDS = 5.0


class CrgEvidenceError(ValueError):
    pass


class EvidenceLedger:
    """Append-only owner-private JSONL with cross-process duplicate suppression."""

    def __init__(self, path: str | Path) -> None:
        self.path = _canonicalize_filesystem_prefix(Path(path).expanduser())
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def append(self, entry: Mapping[str, Any]) -> bool:
        try:
            validate_schema(entry, "evidence-ledger-v1.schema.json")
        except (SchemaValidationError, OSError) as exc:
            raise CrgEvidenceError(str(exc)) from exc
        encoded = canonical_json(entry).encode("utf-8")
        if len(encoded) > MAX_RECORD_BYTES:
            raise CrgEvidenceError("evidence ledger record exceeds 16 KiB")
        if contains_sensitive_plaintext(entry):
            raise CrgEvidenceError("evidence ledger record contains sensitive plaintext")
        _reject_symlink_components(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_symlink_components(self.path.parent)
        self.path.parent.chmod(0o700)
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise CrgEvidenceError("evidence ledger paths must not be symbolic links")
        with _file_lock(self.lock_path):
            evidence_id = entry.get("evidence_id")
            if not isinstance(evidence_id, str):
                raise CrgEvidenceError("evidence record has no evidence_id")
            provenance = entry.get("provenance")
            if (
                not isinstance(provenance, Mapping)
                or provenance.get("source_id") != evidence_id
            ):
                raise CrgEvidenceError(
                    "evidence provenance source_id does not match evidence_id"
                )
            if self.path.exists():
                self.path.chmod(0o600)
                with self.path.open("rb") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        if not line.strip():
                            continue
                        if len(line) > MAX_RECORD_BYTES + 1:
                            raise CrgEvidenceError(
                                f"evidence ledger record {line_number} exceeds 16 KiB"
                            )
                        try:
                            existing = json.loads(line)
                        except (json.JSONDecodeError, UnicodeError) as exc:
                            raise CrgEvidenceError(
                                f"evidence ledger record {line_number} contains invalid JSON"
                            ) from exc
                        if not isinstance(existing, Mapping):
                            raise CrgEvidenceError(
                                f"evidence ledger record {line_number} is not an object"
                            )
                        if existing.get("evidence_id") == evidence_id:
                            return False
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.chmod(self.path, 0o600)
                _write_all(descriptor, encoded + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return True


class CrgEvidenceProvider:
    def __init__(
        self, *, repo_root: str | Path, project_id: str,
        registry_path: str | Path | None = None,
        ledger: EvidenceLedger | None = None,
        revision_resolver: Callable[[Path], str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve(strict=False)
        self.project_id = project_id
        self.registry_path = Path(
            registry_path or Path.home() / ".code-review-graph" / "registry.json"
        ).expanduser()
        self.ledger = ledger or EvidenceLedger(
            self.repo_root / ".agent" / "memory" / "evidence" / "ledger.jsonl"
        )
        self.revision_resolver = revision_resolver or _git_revision

    def health(self) -> dict[str, Any]:
        warnings: list[str] = []
        revision = self.revision_resolver(self.repo_root)
        if not revision:
            warnings.append("missing_repository_revision")
        entry = self._registration(warnings)
        if entry is None:
            return self._health("unavailable", warnings, revision=revision)
        data_dir = self._data_dir(entry)
        durable = not _is_volatile(data_dir)
        if not durable:
            warnings.append("volatile_data_directory")
        database = data_dir / "graph.db"
        if database.is_symlink():
            warnings.append("symlink_graph_database")
            return self._health(
                "unavailable", warnings, revision=revision,
                data_dir=str(data_dir), database=str(database), durable=False,
            )
        if not database.is_file():
            warnings.append("missing_graph_database")
            return self._health(
                "unavailable", warnings, revision=revision,
                data_dir=str(data_dir), durable=durable,
            )
        try:
            database = database.resolve(strict=True)
        except (OSError, RuntimeError):
            warnings.append("unreadable_graph_database_path")
            return self._health(
                "unavailable", warnings, revision=revision,
                data_dir=str(data_dir), durable=False,
            )
        if _is_volatile(database):
            warnings.append("volatile_graph_database")
            return self._health(
                "unavailable", warnings, revision=revision,
                data_dir=str(data_dir), database=str(database), durable=False,
            )
        try:
            with contextlib.closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as conn:
                metadata = dict(conn.execute("select key, value from metadata"))
                nodes = int(conn.execute("select count(*) from nodes").fetchone()[0])
                files = int(conn.execute(
                    "select count(*) from nodes where kind='File'"
                ).fetchone()[0])
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            warnings.append(f"unreadable_graph_database:{type(exc).__name__}")
            return self._health(
                "unavailable", warnings, revision=revision,
                data_dir=str(data_dir), durable=durable,
            )
        if nodes == 0:
            warnings.append("zero_nodes")
        if files == 0:
            warnings.append("zero_files")
        graph_updated = metadata.get("last_updated")
        if not graph_updated:
            warnings.append("missing_graph_timestamp")
        else:
            try:
                _utc(str(graph_updated))
            except CrgEvidenceError:
                warnings.append("invalid_graph_timestamp")
        graph_revision = metadata.get("git_head_sha")
        if not graph_revision:
            warnings.append("missing_graph_revision")
        elif revision and graph_revision != revision:
            warnings.append("revision_mismatch")
        unavailable = any(item in warnings for item in (
            "volatile_data_directory", "zero_nodes", "zero_files",
            "missing_repository_revision", "missing_graph_timestamp",
            "invalid_graph_timestamp", "missing_graph_revision",
        ))
        status = "unavailable" if unavailable else (
            "stale" if "revision_mismatch" in warnings else "healthy"
        )
        return self._health(
            status, warnings, revision=revision, data_dir=str(data_dir),
            database=str(database), durable=durable, nodes=nodes, files=files,
            graph_revision=graph_revision, graph_updated_at=graph_updated,
            schema_version=metadata.get("schema_version"),
            embedding_provider=metadata.get("embedding_provider"),
            embedding_model=metadata.get("embedding_model"),
        )

    def request(
        self, *, operation: str, query: str = "", target: str = "",
    ) -> dict[str, Any]:
        try:
            tool_name = SAFE_TOOLS[operation]
        except KeyError as exc:
            raise CrgEvidenceError(f"unsupported evidence operation: {operation}") from exc
        health = self.health()
        revision = health.get("repository_revision")
        parameters: dict[str, Any] = {"repo_root": str(self.repo_root)}
        if operation == "semantic_search":
            semantic_query = str(query).strip()
            if not semantic_query:
                raise CrgEvidenceError("semantic_search requires a non-empty query")
            parameters["query"] = semantic_query[:500]
        elif operation == "graph_query":
            graph_target = str(target).strip()
            if query not in GRAPH_QUERY_PATTERNS or not graph_target:
                raise CrgEvidenceError(
                    "graph_query requires a supported pattern and non-empty target"
                )
            parameters.update({"pattern": str(query)[:100], "target": graph_target[:500]})
        elif operation in {"impact", "change_review"}:
            parameters["base"] = str(target or "HEAD~1")[:200]
        request_seed = canonical_json({
            "project_id": self.project_id,
            "repository_root": str(self.repo_root),
            "operation": operation, "parameters": parameters,
            "repository_revision": revision,
        })
        warnings = list(health.get("warnings", []))
        status = "ready"
        if health.get("status") != "healthy":
            status = "planned"
            warnings.insert(0, "evidence_unavailable")
        result = {
            "schema": "agentic.memory.evidence-request.v1",
            "request_id": "evr_" + _digest(request_seed)[:32],
            "status": status,
            "operation": operation,
            "tool_name": tool_name,
            "repository_root": str(self.repo_root),
            "repository_revision": revision,
            "parameters": parameters,
            "warnings": warnings,
            "health": health,
        }
        try:
            validate_schema(result, "evidence-request-v1.schema.json")
        except (SchemaValidationError, OSError) as exc:
            raise CrgEvidenceError(str(exc)) from exc
        return result

    def request_for_intent(self, intent: str) -> dict[str, Any]:
        """Select the lightest CRG surface that can answer a routed intent."""
        lowered = intent.lower()
        if "architecture" in lowered:
            return self.request(operation="architecture")
        if any(term in lowered for term in ("impact", "blast radius", "refactor")):
            return self.request(operation="impact")
        if any(term in lowered for term in ("code review", "review changes", "diff")):
            return self.request(operation="change_review")
        patterns = {
            "caller": "callers_of", "callee": "callees_of",
            "import": "importers_of", "test": "tests_for",
        }
        for signal, pattern in patterns.items():
            if signal in lowered:
                return self.request(
                    operation="graph_query", query=pattern, target=intent[:500],
                )
        return self.request(operation="semantic_search", query=intent[:500])

    def record(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "kind", "tool_name", "repository_root", "repository_revision",
            "graph_updated_at", "summary", "confidence_tier", "symbols",
            "relationships", "working_tree",
        }
        extras = set(raw) - allowed
        if extras:
            raise CrgEvidenceError(f"unsupported evidence fields: {', '.join(sorted(extras))}")
        kind = raw.get("kind")
        if kind not in {"crg_node", "crg_flow"}:
            raise CrgEvidenceError("CRG evidence kind must be crg_node or crg_flow")
        tool_name = raw.get("tool_name")
        if tool_name not in CRG_TOOL_NAMES:
            raise CrgEvidenceError("unsupported CRG tool name")
        health = self.health()
        if health["status"] != "healthy":
            raise CrgEvidenceError("CRG graph is unavailable or revision-stale")
        revision = self._validate_repository(raw, health)
        graph_updated = str(raw.get("graph_updated_at") or "")
        if graph_updated != str(health.get("graph_updated_at") or ""):
            raise CrgEvidenceError("graph timestamp does not match current CRG metadata")
        summary = redact(str(raw.get("summary") or "").strip())
        if not summary or len(summary) > MAX_SUMMARY_CHARS:
            raise CrgEvidenceError("evidence summary must be between 1 and 2000 characters")
        confidence_tier = str(raw.get("confidence_tier") or "")
        confidence = {"high": 1.0, "medium": 0.7, "low": 0.4}.get(confidence_tier)
        if confidence is None:
            raise CrgEvidenceError("confidence_tier must be high, medium, or low")
        symbols = raw.get("symbols", [])
        if not isinstance(symbols, list) or len(symbols) > MAX_SYMBOLS:
            raise CrgEvidenceError("symbols must be a list of at most 50 entries")
        validated = self._validate_symbols(symbols, Path(health["database"]))
        if not validated:
            raise CrgEvidenceError("CRG evidence requires at least one qualified symbol")
        relationships = raw.get("relationships", [])
        if not isinstance(relationships, list) or any(not isinstance(item, str) for item in relationships):
            raise CrgEvidenceError("relationships must be a string list")
        if contains_sensitive_plaintext(relationships):
            raise CrgEvidenceError("relationships contain sensitive plaintext")
        working_tree = raw.get("working_tree", False)
        if not isinstance(working_tree, bool):
            raise CrgEvidenceError("working_tree must be boolean")
        locator = {
            "tool_name": tool_name,
            "graph_updated_at": graph_updated,
            "graph_schema_version": health.get("schema_version"),
            "confidence_tier": confidence_tier,
            "symbols": validated,
            "relationships": relationships[:50],
            "executed_test": False,
            "working_tree": working_tree,
        }
        seed = canonical_json({
            "kind": kind, "project_id": self.project_id, "revision": revision,
            "summary": summary, "locator": locator,
        })
        evidence_id = "evi_" + _digest(seed)
        provenance = ProvenanceRef(
            kind=kind, provider="crg", source_id=evidence_id,
            project_id=self.project_id, repository_revision=revision,
            source_hash="sha256:" + _digest(seed),
            observed_at=_utc(graph_updated), confidence=confidence,
            freshness="fresh", locator=locator,
        )
        entry = {
            "schema": "agentic.memory.evidence-ledger.v1",
            "evidence_id": evidence_id,
            "summary": summary,
            "provenance": provenance.to_dict(),
            "verification": {
                "repository_reconciled": True,
                "files_reconciled": True,
                "symbols_reconciled": True,
                "executed_test": False,
            },
        }
        return self._append(entry)

    def record_test_run(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        relation = str(raw.get("source_relation") or "").strip()
        if relation:
            raise CrgEvidenceError(
                f"{relation} is a structural association, not executed-test evidence"
            )
        allowed = {
            "repository_root", "repository_revision", "command_digest",
            "exit_code", "completed_at", "test_ids", "source_relation",
        }
        extras = set(raw) - allowed
        if extras:
            raise CrgEvidenceError(f"unsupported test evidence fields: {', '.join(sorted(extras))}")
        revision = self._validate_repository(raw, None)
        digest = str(raw.get("command_digest") or "")
        if not _sha256_value(digest):
            raise CrgEvidenceError("command_digest must be sha256:<64 hex>")
        exit_code = raw.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise CrgEvidenceError("test exit_code must be an integer")
        test_ids = raw.get("test_ids", [])
        if (
            not isinstance(test_ids, list) or not test_ids or len(test_ids) > 100
            or any(not isinstance(item, str) or not item or len(item) > 500 for item in test_ids)
        ):
            raise CrgEvidenceError("test_ids must contain 1 to 100 bounded identifiers")
        if contains_sensitive_plaintext(test_ids):
            raise CrgEvidenceError("test_ids contain sensitive plaintext")
        completed_at = _utc(str(raw.get("completed_at") or ""))
        locator = {
            "command_digest": digest, "exit_code": exit_code,
            "test_ids": test_ids, "completed_at": completed_at,
            "executed_test": True,
        }
        seed = canonical_json({
            "kind": "test_run", "project_id": self.project_id,
            "revision": revision, "locator": locator,
        })
        evidence_id = "evi_" + _digest(seed)
        provenance = ProvenanceRef(
            kind="test_run", provider="test-runner", source_id=evidence_id,
            project_id=self.project_id, repository_revision=revision,
            source_hash=digest, observed_at=completed_at,
            confidence=1.0, freshness="fresh", locator=locator,
        )
        status = "passed" if exit_code == 0 else "failed"
        entry = {
            "schema": "agentic.memory.evidence-ledger.v1",
            "evidence_id": evidence_id,
            "summary": f"Explicit test run {status}: {len(test_ids)} test identifier(s)",
            "provenance": provenance.to_dict(),
            "verification": {
                "repository_reconciled": True, "files_reconciled": False,
                "symbols_reconciled": False, "executed_test": True,
            },
        }
        return self._append(entry)

    def _append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        recorded = self.ledger.append(entry)
        return {
            "status": "recorded" if recorded else "duplicate",
            "evidence_id": entry["evidence_id"],
        }

    def _validate_repository(
        self, raw: Mapping[str, Any], health: Mapping[str, Any] | None,
    ) -> str:
        try:
            supplied_root = Path(str(raw.get("repository_root"))).expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            raise CrgEvidenceError("evidence repository does not exist") from exc
        if supplied_root != self.repo_root:
            raise CrgEvidenceError("evidence repository does not match active project")
        current = self.revision_resolver(self.repo_root)
        supplied = str(raw.get("repository_revision") or "")
        if not current or supplied != current:
            raise CrgEvidenceError("evidence repository revision does not match current HEAD")
        if health is not None and supplied != health.get("graph_revision"):
            raise CrgEvidenceError("evidence revision does not match CRG graph revision")
        return supplied

    def _validate_symbols(
        self, symbols: list[Any], database: Path,
    ) -> list[dict[str, Any]]:
        validated = []
        with contextlib.closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        ) as conn:
            for raw in symbols:
                if not isinstance(raw, Mapping):
                    raise CrgEvidenceError("symbol evidence entries must be objects")
                qualified = str(raw.get("qualified_name") or "")
                relative = str(raw.get("file_path") or "")
                supplied_hash = str(raw.get("file_hash") or "")
                if not qualified or not relative or not _sha256_value(supplied_hash):
                    raise CrgEvidenceError("symbol evidence is missing qualified name, path, or hash")
                path = (self.repo_root / relative).resolve(strict=False)
                try:
                    path.relative_to(self.repo_root)
                except ValueError as exc:
                    raise CrgEvidenceError("referenced file escapes repository") from exc
                if not path.is_file():
                    raise CrgEvidenceError("referenced file no longer exists")
                disk_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if supplied_hash != "sha256:" + disk_hash:
                    raise CrgEvidenceError("referenced file hash does not match disk")
                row = conn.execute(
                    "select file_path, file_hash from nodes where qualified_name=?",
                    (qualified,),
                ).fetchone()
                if row is None:
                    raise CrgEvidenceError("referenced symbol no longer exists in CRG")
                if row[0] != relative:
                    raise CrgEvidenceError("symbol file path does not match CRG")
                if row[1] and row[1] != disk_hash:
                    raise CrgEvidenceError("CRG file hash does not match disk")
                validated.append({
                    "qualified_name": qualified, "file_path": relative,
                    "file_hash": supplied_hash,
                })
        return validated

    def _registration(self, warnings: list[str]) -> Mapping[str, Any] | None:
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
            repos = value.get("repos", [])
            if not isinstance(repos, list):
                raise TypeError("repos is not a list")
        except FileNotFoundError:
            warnings.append("missing_crg_registry")
            return None
        except (json.JSONDecodeError, OSError, TypeError, UnicodeError):
            warnings.append("invalid_crg_registry")
            return None
        for entry in repos:
            if not isinstance(entry, Mapping):
                continue
            try:
                registered = Path(str(entry.get("path"))).expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if registered == self.repo_root:
                return entry
        warnings.append("repository_not_registered")
        return None

    def _data_dir(self, entry: Mapping[str, Any]) -> Path:
        value = entry.get("data_dir")
        if value:
            return Path(str(value)).expanduser().resolve(strict=False)
        alias = str(entry.get("alias") or self.repo_root.name)
        return (self.registry_path.parent / "repos" / alias).resolve(strict=False)

    @staticmethod
    def _health(status: str, warnings: list[str], **values: Any) -> dict[str, Any]:
        return {
            "status": status, "provider": "crg", "warnings": warnings,
            "nodes": None, "files": None, "graph_revision": None,
            "graph_updated_at": None, "schema_version": None,
            "embedding_provider": None, "embedding_model": None,
            "data_dir": None, "database": None, "durable": False,
            "repository_revision": values.pop("revision", None),
            **values,
        }


def _git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            capture_output=True, timeout=1.0,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _is_volatile(path: Path) -> bool:
    absolute = path.absolute()
    return any(absolute == root or root in absolute.parents for root in VOLATILE_ROOTS)


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinks in every ledger parent below the canonical top level."""
    current = path.absolute()
    while current != current.parent:
        if current.is_symlink():
            raise CrgEvidenceError(
                f"evidence ledger parent must not be a symbolic link: {current}"
            )
        current = current.parent


def _canonicalize_filesystem_prefix(path: Path) -> Path:
    """Resolve only a platform top-level alias (for example macOS /var)."""
    absolute = path.absolute()
    parts = absolute.parts
    if len(parts) < 2:
        return absolute
    top_level = Path(absolute.anchor) / parts[1]
    if not top_level.is_symlink():
        return absolute
    try:
        canonical_top = top_level.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CrgEvidenceError(
            f"cannot resolve filesystem prefix for evidence ledger: {top_level}"
        ) from exc
    return canonical_top.joinpath(*parts[2:])


def _sha256_value(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(char in "0123456789abcdef" for char in value[7:])


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CrgEvidenceError("evidence timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short evidence ledger write")
        view = view[written:]


@contextlib.contextmanager
def _file_lock(path: Path):
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    os.chmod(path, 0o600)
    stream = os.fdopen(descriptor, "a+")
    try:
        try:
            import fcntl
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CrgEvidenceError("timed out acquiring evidence ledger lock")
                    threading.Event().wait(0.05)
            unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except ImportError:
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write("\0")
                stream.flush()
            stream.seek(0)
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CrgEvidenceError("timed out acquiring evidence ledger lock")
                    threading.Event().wait(0.05)

            def unlock() -> None:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        yield
    finally:
        try:
            if "unlock" in locals():
                unlock()
        except OSError:
            pass
        stream.close()
