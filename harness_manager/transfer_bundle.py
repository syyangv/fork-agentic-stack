"""Portable transfer bundle export/import for agentic-stack memory."""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
SENTINEL = "## Auto-promoted entries will be appended below"
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 16 * 1024 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_RECORD_BYTES = 16 * 1024
MAX_EVIDENCE_RECORDS = 1000
MAX_LESSON_RECORD_BYTES = 16 * 1024
MAX_LESSON_RECORDS = 1000
TRANSFER_SCOPES = frozenset({
    "preferences",
    "decisions",
    "accepted_lessons",
    "evidence_ledger",
    "skills",
    "working",
    "episodic",
    "candidates",
    "data_layer",
    "flywheel",
})


class BundleSecurityError(ValueError):
    """Raised when a transfer bundle would include high-risk content."""


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:OPENAI|ANTHROPIC|GITHUB|GH|AWS|GOOGLE)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\s*="),
)

RUNTIME_PARTS = {
    ".index",
    ".cache",
    ".code-review-graph",
    ".pytest_cache",
    "cache",
    "snapshots",
    "exports",
    "__pycache__",
    "runtime",
}
RUNTIME_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".tmp",
}
CRG_DERIVED_PARTS = frozenset({
    ".code-review-graph", ".crg", "code-review-graph", "code_review_graph",
    "crg", "crg-cache", "crg_cache", "graph-cache", "graph_cache",
    "graph-registry", "graph_registry",
})
CRG_DERIVED_COMPACT_MARKERS = frozenset({
    "codereviewgraph", "crg", "crgcache", "graphcache", "crgregistry",
    "graphregistry", "crgindex", "graphindex", "crgnodes", "graphnodes",
    "crgedges", "graphedges", "crgmetadata", "graphmetadata", "crgstate",
    "graphstate", "crgwal", "crgshm", "crgsnapshot", "graphsnapshot",
    "crgdata", "crgstore", "graphstore",
})
CRG_DERIVED_SUFFIXES = frozenset({
    ".graph", ".graphdb", ".duckdb", ".lmdb", ".snapshot", ".idx",
    ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm",
})


@dataclass(frozen=True)
class ImportPlan:
    """A fully validated set of destination writes.

    Constructing a plan is strictly read-only. Applying one writes only paths
    whose final bytes differ, so a repeated import is byte/mtime idempotent.
    """

    target_root: Path
    writes: tuple[tuple[str, bytes], ...]
    result: dict[str, Any]
    bundle_fingerprint: str
    baseline: tuple[tuple[str, tuple[object, ...]], ...]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def scan_text_for_secrets(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def export_bundle(
    agent_root: Path | str,
    targets: Iterable[str],
    scopes: Iterable[str],
    project_name: str | None = None,
) -> dict[str, Any]:
    agent_root = Path(agent_root)
    scopes = list(scopes)
    unknown_scopes = sorted(set(scopes) - TRANSFER_SCOPES)
    if unknown_scopes:
        raise ValueError(f"unsupported transfer scope(s): {', '.join(unknown_scopes)}")
    bundle: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "source": {
            "agentic_stack_version": _version(),
            "project_name": project_name or agent_root.parent.name or "unknown",
        },
        "targets": list(targets),
        "scopes": scopes,
        "files": [],
        "lessons": [],
        "evidence": [],
        "warnings": [],
    }

    if "preferences" in scopes:
        _add_file(bundle, agent_root, agent_root / "memory" / "personal" / "PREFERENCES.md")

    if "decisions" in scopes:
        _add_file(bundle, agent_root, agent_root / "memory" / "semantic" / "DECISIONS.md")

    if "accepted_lessons" in scopes:
        bundle["lessons"] = _load_accepted_lessons(agent_root)

    if "evidence_ledger" in scopes:
        bundle["evidence"] = _load_evidence_ledger(
            agent_root / "memory" / "evidence" / "ledger.jsonl",
            agent_root=agent_root,
        )

    if "skills" in scopes:
        skills_root = agent_root / "skills"
        _add_tree(bundle, agent_root, skills_root)

    if "working" in scopes:
        _add_tree(bundle, agent_root, agent_root / "memory" / "working")

    if "episodic" in scopes:
        _add_tree(bundle, agent_root, agent_root / "memory" / "episodic")

    if "candidates" in scopes:
        _add_tree(bundle, agent_root, agent_root / "memory" / "candidates")

    return bundle


def encode_bundle(bundle: dict[str, Any]) -> tuple[str, str]:
    raw = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    digest = hashlib.sha256(compressed).hexdigest()
    payload = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return payload, digest


def decode_payload(payload: str, digest: str) -> dict[str, Any]:
    if len(payload) > (MAX_BUNDLE_BYTES * 2):
        raise ValueError("transfer payload exceeds encoded size bound")
    padded = payload + ("=" * (-len(payload) % 4))
    try:
        compressed = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("transfer payload is not valid base64") from exc
    if len(compressed) > MAX_BUNDLE_BYTES:
        raise ValueError("transfer payload exceeds compressed size bound")
    actual = hashlib.sha256(compressed).hexdigest()
    if actual != digest:
        raise ValueError("transfer payload digest mismatch")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise ValueError("transfer payload is not valid gzip data") from exc
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("transfer payload exceeds decoded size bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("transfer payload is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("transfer payload must decode to an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported transfer schema: {data.get('schema_version')}")
    return data


def import_bundle(bundle: dict[str, Any], target_root: Path | str) -> dict[str, Any]:
    """Preflight and transactionally apply one bounded transfer bundle."""
    return apply_import_plan(preflight_import(bundle, target_root))


def preflight_import(
    bundle: dict[str, Any],
    target_root: Path | str,
) -> ImportPlan:
    """Validate a whole bundle and compute final bytes without mutating disk."""
    if not isinstance(bundle, dict):
        raise ValueError("transfer bundle must be an object")
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported transfer schema: {bundle.get('schema_version')}")

    target_root = validate_target_root(target_root)
    scopes = bundle.get("scopes", [])
    if (
        not isinstance(scopes, list)
        or any(not isinstance(scope, str) for scope in scopes)
    ):
        raise ValueError("transfer scopes must be a list of strings")
    unknown_scopes = sorted(set(scopes) - TRANSFER_SCOPES)
    if unknown_scopes:
        raise ValueError(f"unsupported transfer scope(s): {', '.join(unknown_scopes)}")
    targets = bundle.get("targets", [])
    if (
        not isinstance(targets, list)
        or any(not isinstance(target, str) or not target for target in targets)
    ):
        raise ValueError("transfer targets must be a list of non-empty strings")

    result: dict[str, Any] = {
        "files_imported": 0,
        "preferences_imported": False,
        "decisions_imported": False,
        "lessons_imported": 0,
        "evidence_imported": 0,
        "skills_imported": 0,
        "crg_rebuild_required": True,
        "crg_next_action": "Rebuild CRG graph locally after import; no CRG graph database or cache was transferred.",
    }
    writes: dict[str, bytes] = {}
    files = bundle.get("files", [])
    if not isinstance(files, list):
        raise ValueError("transfer files must be a list")
    if len(files) > 10_000:
        raise ValueError("transfer file count exceeds bound")
    total_file_bytes = 0
    seen_files: dict[str, bytes] = {}
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"transfer file entry {index} must be an object")
        if set(entry) - {"path", "encoding", "content_b64"}:
            raise ValueError(f"transfer file entry {index} has unsupported fields")
        relative_text = entry.get("path")
        if not isinstance(relative_text, str) or not relative_text:
            raise ValueError(f"transfer file entry {index} has no path")
        rel = Path(relative_text)
        _ensure_allowed(rel)
        _ensure_scope_path(rel, set(scopes))
        if entry.get("encoding", "utf-8") != "utf-8":
            raise ValueError(f"transfer file {relative_text} must use UTF-8")
        encoded = entry.get("content_b64")
        if not isinstance(encoded, str):
            raise ValueError(f"transfer file {relative_text} has invalid content")
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
            content = raw.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise ValueError(
                f"transfer file {relative_text} is not valid base64 UTF-8"
            ) from exc
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"transfer file {relative_text} exceeds size bound")
        total_file_bytes += len(raw)
        if total_file_bytes > MAX_TOTAL_FILE_BYTES:
            raise ValueError("transfer file content exceeds total size bound")
        if scan_text_for_secrets(content):
            raise BundleSecurityError(
                f"secret-like content detected in {relative_text}"
            )
        prior = seen_files.get(rel.as_posix())
        if prior is not None and prior != raw:
            raise ValueError(f"conflicting duplicate transfer file: {relative_text}")
        seen_files[rel.as_posix()] = raw

    for relative_text, raw in sorted(seen_files.items()):
        rel = Path(relative_text)
        existing = _destination_bytes(target_root, rel)
        if relative_text == ".agent/memory/personal/PREFERENCES.md":
            final = _merge_markdown(existing, raw, "Imported Preferences")
            result["preferences_imported"] = final != existing
        elif relative_text == ".agent/memory/semantic/DECISIONS.md":
            final = _merge_markdown(existing, raw, "Imported Decisions")
            result["decisions_imported"] = final != existing
        else:
            final = raw
        if final != existing:
            writes[relative_text] = final
            result["files_imported"] += 1
            if relative_text.startswith(".agent/skills/"):
                result["skills_imported"] += 1

    lessons = bundle.get("lessons", [])
    if not isinstance(lessons, list):
        raise ValueError("transfer lessons must be a list")
    if lessons and "accepted_lessons" not in scopes:
        raise ValueError("transfer lessons require the accepted_lessons scope")
    lesson_rel = Path(".agent/memory/semantic/lessons.jsonl")
    lesson_existing = _destination_bytes(target_root, lesson_rel)
    lesson_bytes, lessons_imported, final_lesson_rows = _merge_lessons(
        lesson_existing, lessons
    )
    result["lessons_imported"] = lessons_imported
    if lesson_bytes != lesson_existing:
        writes[lesson_rel.as_posix()] = lesson_bytes
    if lessons or lesson_existing is not None:
        lessons_md_rel = Path(".agent/memory/semantic/LESSONS.md")
        current_md = _destination_bytes(target_root, lessons_md_rel)
        rendered = _render_lessons_bytes(final_lesson_rows, current_md)
        if rendered != current_md:
            writes[lessons_md_rel.as_posix()] = rendered

    evidence = bundle.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("transfer evidence must be a list")
    if evidence and "evidence_ledger" not in scopes:
        raise ValueError("transfer evidence requires the evidence_ledger scope")
    evidence_rel = Path(".agent/memory/evidence/ledger.jsonl")
    evidence_existing = _destination_bytes(target_root, evidence_rel)
    evidence_bytes, evidence_imported = _merge_evidence(
        evidence_existing, evidence
    )
    result["evidence_imported"] = evidence_imported
    if evidence_bytes != evidence_existing:
        writes[evidence_rel.as_posix()] = evidence_bytes

    fingerprint = _content_identity(bundle)
    record_rel = Path(f".agent/transfer/imports/{fingerprint}.json")
    record_existing = _destination_bytes(target_root, record_rel)
    if record_existing is None:
        record = {
            "bundle_sha256": fingerprint,
            "bundle_created_at": bundle.get("created_at"),
            "targets": targets,
            "scopes": scopes,
            "result": result,
        }
        writes[record_rel.as_posix()] = (
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    return ImportPlan(
        target_root=target_root,
        writes=tuple(sorted(writes.items())),
        result=dict(result),
        bundle_fingerprint=fingerprint,
        baseline=tuple(sorted(_capture_baseline(target_root, writes).items())),
    )


def validate_target_root(target_root: Path | str) -> Path:
    """Reject an unsafe transfer root before inspecting or creating children.

    Import preflight reads destination data and the bootstrap later writes a
    trusted template, so a symbolic-link (or non-directory) root must be
    rejected before either operation.  The descriptor open is deliberately
    no-follow as a second, race-resistant check; callers that subsequently
    mutate still use descriptor-relative operations.
    """
    root = Path(target_root)
    try:
        info = root.lstat()
    except FileNotFoundError as exc:
        raise ValueError("transfer target root must be an existing directory") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("transfer target root must not be symbolic")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("transfer target root must be a directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise ValueError("transfer target root must be a real directory, not symbolic") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("transfer target root changed during validation")
    finally:
        os.close(descriptor)
    return root


def apply_import_plan(
    plan: ImportPlan, *, fault: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Apply a fresh plan through no-follow directory descriptors only."""
    if not isinstance(plan, ImportPlan):
        raise TypeError("apply_import_plan requires a validated ImportPlan")
    _assert_plan_fresh(plan)
    staged: list[tuple[str, str, int, Path]] = []
    originals: dict[Path, tuple[bytes, int, int] | None] = {}
    created_dirs: list[Path] = []
    try:
        for relative_text, content in plan.writes:
            destination = plan.target_root / relative_text
            parent_fd = _open_safe_parent(
                plan.target_root, Path(relative_text).parent, created_dirs
            )
            try:
                mode, original = _read_existing_at(parent_fd, destination.name, relative_text)
                originals[destination] = original
                temporary = f".{destination.name}.transfer-{uuid.uuid4().hex}"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    mode,
                    dir_fd=parent_fd,
                )
                try:
                    os.fchmod(descriptor, mode)
                    _write_all(descriptor, content)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except Exception:
                os.close(parent_fd)
                raise
            staged.append((temporary, destination.name, parent_fd, destination))

        replaced: list[tuple[str, str, int, Path]] = []
        try:
            for temporary, destination_name, parent_fd, destination in staged:
                _assert_destination_guard(plan, destination)
                if fault is not None:
                    fault("before_import_replace")
                os.replace(temporary, destination_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                replaced.append((temporary, destination_name, parent_fd, destination))
                if fault is not None:
                    fault("after_import_replace")
        except Exception:
            for _temporary, destination_name, parent_fd, destination in reversed(replaced):
                original = originals[destination]
                if original is None:
                    try:
                        os.unlink(destination_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                else:
                    _replace_at(
                        parent_fd,
                        destination_name,
                        original[0],
                        original[1],
                        original[2],
                    )
            raise
    except Exception:
        for temporary, _destination_name, parent_fd, _destination in staged:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent_fd)
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    for _temporary, _destination_name, parent_fd, _destination in staged:
        os.close(parent_fd)
    return dict(plan.result)


def _open_safe_parent(
    root: Path, relative_parent: Path, created_dirs: list[Path]
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    current = root
    try:
        for part in relative_parent.parts:
            if part in ("", "."):
                continue
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                current = current / part
                created_dirs.append(current)
                child = os.open(part, flags, dir_fd=descriptor)
            else:
                current = current / part
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_existing_at(
    parent_fd: int, name: str, relative_text: str
) -> tuple[int, tuple[bytes, int, int] | None]:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
        )
    except FileNotFoundError:
        return 0o600, None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LEDGER_BYTES:
            raise ValueError(f"transfer destination is not a safe regular file: {relative_text}")
        return stat.S_IMODE(info.st_mode), (
            _read_descriptor(descriptor, info.st_size),
            stat.S_IMODE(info.st_mode),
            info.st_mtime_ns,
        )
    finally:
        os.close(descriptor)


def _read_descriptor(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > expected_size:
        raise ValueError("transfer destination changed while being read")
    return raw


def _replace_at(
    parent_fd: int, name: str, content: bytes, mode: int, mtime_ns: int
) -> None:
    temporary = f".{name}.rollback-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.utime(
        temporary,
        ns=(mtime_ns, mtime_ns),
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _content_identity(bundle: dict[str, Any]) -> str:
    """Stable import identity over all authoritative bundle content.

    ``created_at`` is an export timestamp, not transferred authority. Every
    other field, including source provenance, targets, scopes, and records,
    remains in the identity.
    """
    identity = dict(bundle)
    identity.pop("created_at", None)
    return hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()


def _capture_baseline(
    target_root: Path, writes: dict[str, bytes]
) -> dict[str, tuple[object, ...]]:
    paths = {"."}
    for relative_text in writes:
        current = Path(relative_text)
        while current.parts:
            paths.add(current.as_posix())
            current = current.parent
            if current == Path("."):
                break
    return {
        relative_text: _path_signature(
            target_root if relative_text == "." else target_root / relative_text
        )
        for relative_text in paths
    }


def _path_signature(path: Path) -> tuple[object, ...]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ("missing",)
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", info.st_dev, info.st_ino, os.readlink(path))
    if stat.S_ISDIR(info.st_mode):
        return ("directory", info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    if not stat.S_ISREG(info.st_mode):
        return ("other", info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))
    if info.st_size > MAX_LEDGER_BYTES:
        return ("file-too-large", info.st_dev, info.st_ino, info.st_size)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return ("raced",)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return (
            "file", info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode),
            info.st_mtime_ns, info.st_size, digest.hexdigest(),
        )
    finally:
        os.close(descriptor)


def _assert_plan_fresh(plan: ImportPlan) -> None:
    for relative_text, expected in plan.baseline:
        path = plan.target_root if relative_text == "." else plan.target_root / relative_text
        actual = _path_signature(path)
        if actual != expected:
            raise ValueError(f"stale transfer plan at {relative_text}")
        if actual[0] == "symlink":
            raise ValueError(f"transfer destination path became symbolic: {relative_text}")


def _assert_destination_guard(plan: ImportPlan, destination: Path) -> None:
    baseline = dict(plan.baseline)
    relative = destination.relative_to(plan.target_root)
    expected = baseline.get(relative.as_posix())
    if expected is not None:
        actual = _path_signature(destination)
        if expected[0] == "missing":
            if actual[0] != "missing":
                raise ValueError(f"stale transfer plan at {relative.as_posix()}")
        elif actual != expected:
            raise ValueError(f"stale transfer plan at {relative.as_posix()}")
    current = relative.parent
    while current != Path("."):
        expected_parent = baseline.get(current.as_posix())
        parent = plan.target_root / current
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"transfer destination path became symbolic: {current}")
        if expected_parent is not None and expected_parent[0] == "directory":
            actual_parent = _path_signature(parent)
            if actual_parent[:3] != expected_parent[:3]:
                raise ValueError(f"stale transfer parent at {current}")
        current = current.parent


def _destination_bytes(target_root: Path, relative: Path) -> bytes | None:
    destination = target_root / relative
    if destination.exists() or destination.is_symlink():
        _reject_destination_symlinks(target_root, relative)
        if relative.as_posix() == ".agent/memory/evidence/ledger.jsonl":
            return _read_safe_source_file(
                destination,
                root=target_root,
                max_bytes=MAX_LEDGER_BYTES,
                label="destination evidence ledger",
            )
        info = destination.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(
                f"transfer destination is not a regular file: {relative.as_posix()}"
            )
        if info.st_size > MAX_LEDGER_BYTES:
            raise ValueError(
                f"transfer destination exceeds read bound: {relative.as_posix()}"
            )
        return destination.read_bytes()
    _reject_destination_symlinks(target_root, relative, allow_missing=True)
    return None


def _reject_destination_symlinks(
    target_root: Path, relative: Path, *, allow_missing: bool = False
) -> None:
    current = target_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"transfer destination path contains a symbolic link: "
                f"{relative.as_posix()}"
            )
        if not current.exists() and allow_missing:
            return


def _ensure_scope_path(path: Path, scopes: set[str]) -> None:
    text = path.as_posix()
    allowed_scope: str | None = None
    if text == ".agent/memory/personal/PREFERENCES.md":
        allowed_scope = "preferences"
    elif text == ".agent/memory/semantic/DECISIONS.md":
        allowed_scope = "decisions"
    elif text.startswith(".agent/skills/"):
        allowed_scope = "skills"
    elif text.startswith(".agent/memory/working/"):
        allowed_scope = "working"
    elif text.startswith(".agent/memory/episodic/"):
        allowed_scope = "episodic"
    elif text.startswith(".agent/memory/candidates/"):
        allowed_scope = "candidates"
    if allowed_scope is None or allowed_scope not in scopes:
        raise ValueError(
            f"transfer path is not allowed by its declared scope: {text}"
        )


def _merge_markdown(
    existing: bytes | None, imported: bytes, heading: str
) -> bytes:
    if existing is None:
        return imported
    if imported.strip() in existing:
        return existing
    digest = hashlib.sha256(imported).hexdigest()[:12]
    marker = f"## {heading} ({digest})".encode("utf-8")
    if marker in existing:
        return existing
    return (
        existing.rstrip()
        + b"\n\n"
        + marker
        + b"\n\n"
        + imported.strip()
        + b"\n"
    )


def _merge_lessons(
    existing: bytes | None, imported: list[Any]
) -> tuple[bytes | None, int, list[dict[str, Any]]]:
    if len(imported) > MAX_LESSON_RECORDS:
        raise ValueError("accepted lesson transfer exceeds record bound")
    existing_rows = _parse_jsonl_bytes(
        existing, label="destination lessons", max_bytes=MAX_LEDGER_BYTES
    )
    latest: dict[str, dict[str, Any]] = {}
    for row in existing_rows:
        if row.get("status") == "accepted":
            _validate_accepted_lesson(row, "destination accepted lesson")
        lesson_id = row.get("id")
        if isinstance(lesson_id, str) and lesson_id:
            latest[lesson_id] = row

    incoming: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, row in enumerate(imported):
        _validate_accepted_lesson(row, f"accepted lesson {index}")
        lesson_id = row["id"]
        prior = incoming.get(lesson_id)
        if prior is not None:
            if _canonical(prior) != _canonical(row):
                raise ValueError(
                    f"accepted lesson conflict inside bundle for id {lesson_id}"
                )
            continue
        incoming[lesson_id] = row
        order.append(lesson_id)

    additions: list[dict[str, Any]] = []
    for lesson_id in order:
        row = incoming[lesson_id]
        prior = latest.get(lesson_id)
        if prior is not None:
            if _canonical(prior) != _canonical(row):
                raise ValueError(
                    f"accepted lesson conflict with destination for id {lesson_id}"
                )
            continue
        additions.append(row)
        latest[lesson_id] = row

    if not additions:
        return existing, 0, existing_rows
    prefix = existing or b""
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    appended = b"".join(
        (_canonical(row) + "\n").encode("utf-8") for row in additions
    )
    final_rows = existing_rows + additions
    return prefix + appended, len(additions), final_rows


def _merge_evidence(
    existing: bytes | None, imported: list[Any]
) -> tuple[bytes | None, int]:
    if len(imported) > MAX_EVIDENCE_RECORDS:
        raise ValueError("evidence transfer exceeds record bound")
    existing_rows = _parse_evidence_bytes(
        existing, label="destination evidence ledger"
    )
    destination_by_id: dict[str, dict[str, Any]] = {}
    for row in existing_rows:
        evidence_id = row["evidence_id"]
        prior = destination_by_id.get(evidence_id)
        if prior is not None and _canonical(prior) != _canonical(row):
            raise ValueError(
                f"destination evidence conflict for id {evidence_id}"
            )
        destination_by_id[evidence_id] = row

    incoming_by_id: dict[str, dict[str, Any]] = {}
    incoming_order: list[str] = []
    for index, row in enumerate(imported):
        validated = _validate_evidence_record(row, f"evidence record {index}")
        evidence_id = validated["evidence_id"]
        prior = incoming_by_id.get(evidence_id)
        if prior is not None:
            if _canonical(prior) != _canonical(validated):
                raise ValueError(
                    f"evidence conflict inside bundle for id {evidence_id}"
                )
            continue
        incoming_by_id[evidence_id] = validated
        incoming_order.append(evidence_id)

    additions: list[dict[str, Any]] = []
    for evidence_id in incoming_order:
        row = incoming_by_id[evidence_id]
        prior = destination_by_id.get(evidence_id)
        if prior is not None:
            if _canonical(prior) != _canonical(row):
                raise ValueError(
                    f"evidence conflict with destination for id {evidence_id}"
                )
            continue
        additions.append(row)

    if not additions:
        return existing, 0
    prefix = existing or b""
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    appended = b"".join(
        (_canonical(row) + "\n").encode("utf-8") for row in additions
    )
    final = prefix + appended
    if len(final) > MAX_LEDGER_BYTES:
        raise ValueError("merged evidence ledger exceeds size bound")
    return final, len(additions)


def _parse_jsonl_bytes(
    value: bytes | None, *, label: str, max_bytes: int
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if len(value) > max_bytes:
        raise ValueError(f"{label} exceeds size bound")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError(
                f"{label} line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {line_number} is not an object")
        rows.append(row)
    return rows


def _parse_evidence_bytes(
    value: bytes | None, *, label: str
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if len(value) > MAX_LEDGER_BYTES:
        raise ValueError(f"{label} exceeds size bound")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line) > MAX_EVIDENCE_RECORD_BYTES:
            raise ValueError(
                f"{label} line {line_number} exceeds record size bound"
            )
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError(
                f"{label} line {line_number} is invalid JSON"
            ) from exc
        rows.append(
            _validate_evidence_record(
                row, f"{label} line {line_number}"
            )
        )
    return rows


def _validate_accepted_lesson(value: Any, label: str) -> dict[str, Any]:
    """Validate authority fields while preserving compatible audit metadata."""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    encoded = _canonical(value).encode("utf-8")
    if len(encoded) > MAX_LESSON_RECORD_BYTES:
        raise ValueError(f"{label} exceeds record size bound")
    lesson_id = value.get("id")
    if (
        not isinstance(lesson_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", lesson_id) is None
    ):
        raise ValueError(f"{label} has invalid id")
    if value.get("status") != "accepted":
        raise ValueError(f"{label} is not accepted governance")
    claim = value.get("claim")
    if not isinstance(claim, str) or not claim.strip() or len(claim) > 2_000:
        raise ValueError(f"{label} has invalid claim")
    for field, maximum, item_maximum in (
        ("conditions", 64, 200),
        ("evidence_ids", 128, 200),
    ):
        items = value.get(field, [])
        if (
            not isinstance(items, list)
            or len(items) > maximum
            or any(
                not isinstance(item, str) or not item or len(item) > item_maximum
                for item in items
            )
        ):
            raise ValueError(f"{label} has invalid {field}")
    if scan_text_for_secrets(encoded.decode("utf-8")):
        raise BundleSecurityError(f"secret-like content detected in {label}")
    return value


def _validate_evidence_record(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    _require_exact_keys(
        value,
        {"schema", "evidence_id", "summary", "provenance", "verification"},
        label,
    )
    if value["schema"] != "agentic.memory.evidence-ledger.v1":
        raise ValueError(f"{label} has unsupported evidence schema")
    evidence_id = value["evidence_id"]
    if (
        not isinstance(evidence_id, str)
        or re.fullmatch(r"evi_[0-9a-f]{64}", evidence_id) is None
    ):
        raise ValueError(f"{label} has invalid evidence_id")
    summary = value["summary"]
    if not isinstance(summary, str) or not 1 <= len(summary) <= 2000:
        raise ValueError(f"{label} has invalid summary")

    provenance = value["provenance"]
    if not isinstance(provenance, dict):
        raise ValueError(f"{label} provenance is not an object")
    _require_exact_keys(
        provenance,
        {
            "kind",
            "provider",
            "source_id",
            "project_id",
            "repository_revision",
            "source_hash",
            "observed_at",
            "confidence",
            "freshness",
            "locator",
        },
        f"{label} provenance",
    )
    if provenance["kind"] not in {"crg_node", "crg_flow", "test_run"}:
        raise ValueError(f"{label} has invalid evidence kind")
    if provenance["provider"] not in {"crg", "test-runner"}:
        raise ValueError(f"{label} has invalid evidence provider")
    if provenance["source_id"] != evidence_id:
        raise ValueError(f"{label} evidence source_id does not match evidence_id")
    if (
        not isinstance(provenance["project_id"], str)
        or re.fullmatch(r"[0-9a-f]{16}", provenance["project_id"]) is None
    ):
        raise ValueError(f"{label} has invalid evidence project_id")
    if (
        not isinstance(provenance["repository_revision"], str)
        or re.fullmatch(
            r"[0-9a-f]{7,64}", provenance["repository_revision"]
        )
        is None
    ):
        raise ValueError(f"{label} has invalid evidence revision")
    if (
        not isinstance(provenance["source_hash"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", provenance["source_hash"])
        is None
    ):
        raise ValueError(f"{label} has invalid evidence source_hash")
    if (
        not isinstance(provenance["observed_at"], str)
        or len(provenance["observed_at"]) > 64
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[^ ]+(?:Z|\+00:00)",
            provenance["observed_at"],
        )
        is None
    ):
        raise ValueError(f"{label} has invalid evidence timestamp")
    confidence = provenance["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= confidence <= 1.0
    ):
        raise ValueError(f"{label} has invalid evidence confidence")
    if provenance["freshness"] != "fresh":
        raise ValueError(f"{label} evidence is not fresh")
    if not isinstance(provenance["locator"], dict):
        raise ValueError(f"{label} evidence locator is not an object")

    verification = value["verification"]
    if not isinstance(verification, dict):
        raise ValueError(f"{label} verification is not an object")
    _require_exact_keys(
        verification,
        {
            "repository_reconciled",
            "files_reconciled",
            "symbols_reconciled",
            "executed_test",
        },
        f"{label} verification",
    )
    if any(type(verification[key]) is not bool for key in verification):
        raise ValueError(f"{label} evidence verification flags must be booleans")

    encoded = _canonical(value).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_RECORD_BYTES:
        raise ValueError(f"{label} exceeds evidence record size bound")
    if scan_text_for_secrets(encoded.decode("utf-8")):
        raise BundleSecurityError(f"secret-like content detected in {label}")
    return value


def _require_exact_keys(
    value: dict[str, Any], expected: set[str], label: str
) -> None:
    missing = sorted(expected - set(value))
    extras = sorted(set(value) - expected)
    if missing or extras:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extras:
            details.append(f"unknown {extras}")
        raise ValueError(f"{label} fields are invalid: {', '.join(details)}")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _render_lessons_bytes(
    rows: list[dict[str, Any]], current: bytes | None
) -> bytes:
    if current is None:
        prefix = (
            "# Lessons\n\n"
            "> _Auto-managed below. Hand-curated preamble + seed lessons "
            "above the sentinel are preserved across renders._\n\n"
            f"{SENTINEL}\n"
        )
    else:
        text = current.decode("utf-8")
        if SENTINEL in text:
            prefix = text.split(SENTINEL, 1)[0].rstrip() + "\n\n" + SENTINEL + "\n"
        else:
            prefix = text.rstrip() + "\n\n" + SENTINEL + "\n"

    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        if row.get("status") == "accepted":
            _validate_accepted_lesson(
                row, f"accepted lesson ledger row {index}"
            )
        lesson_id = row.get("id")
        key = lesson_id if isinstance(lesson_id, str) and lesson_id else f"_anon_{index}"
        if key not in latest:
            order.append(key)
        latest[key] = row
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key in order:
        row = latest[key]
        if row.get("status") != "accepted":
            continue
        month = str(row.get("accepted_at") or "")[:7] or "unknown"
        groups[month].append(row)
    lines: list[str] = []
    for month in sorted(groups, reverse=True):
        lines.extend((f"### {month}", ""))
        for row in groups[month]:
            claim = row.get("claim", "")
            lesson_id = row.get("id", "?")
            evidence_count = len(row.get("evidence_ids", []))
            lines.append(
                f"- {claim}  <!-- status=accepted evidence={evidence_count} "
                f"id={lesson_id} -->"
            )
        lines.append("")
    section = "\n".join(lines).rstrip()
    return (prefix.rstrip() + ("\n\n" + section if section else "") + "\n").encode(
        "utf-8"
    )


def _load_evidence_ledger(path: Path, *, agent_root: Path) -> list[dict[str, Any]]:
    """Validate the complete append ledger, then retain its newest 1,000 rows.

    "Newest" is deterministic append-ledger order: later JSONL lines are newer.
    No timestamp reordering is performed, and every earlier row is still
    validated before the bounded tail is selected.
    """
    if not path.exists() and not path.is_symlink():
        return []
    raw = _read_safe_source_file(
        path, root=agent_root, max_bytes=MAX_LEDGER_BYTES,
        label="evidence ledger",
    )
    rows = _parse_evidence_bytes(raw, label="evidence ledger")
    return list(deque(rows, maxlen=MAX_EVIDENCE_RECORDS))


def _read_safe_source_file(
    path: Path, *, root: Path, max_bytes: int, label: str
) -> bytes:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside the agent root") from exc
    current = root
    if root.is_symlink():
        raise ValueError(f"{label} root must not be symbolic")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} path must not be symbolic")
    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely") from exc
    try:
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or opened.st_mode & 0o022
            or opened.st_size > max_bytes
        ):
            raise ValueError(f"{label} is unsafe or exceeds its size bound")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"{label} exceeds its size bound")
        return raw
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_parents(path: Path, created: list[Path]) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError(f"transfer parent is symbolic: {current}")
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise ValueError(f"transfer parent is unsafe: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        created.append(directory)


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short transfer write")
        view = view[written:]


def _atomic_write(path: Path, value: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.transfer-rollback-", dir=path.parent
    )
    temp_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, value)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temp_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def _add_file(bundle: dict[str, Any], agent_root: Path, path: Path) -> None:
    if not path.is_file():
        return
    rel = Path(".agent") / path.relative_to(agent_root)
    _ensure_allowed(rel)
    if _is_runtime_path(rel):
        raise ValueError(f"runtime/database/cache path is not transferable: {rel}")
    raw = _read_safe_source_file(
        path, root=agent_root, max_bytes=MAX_FILE_BYTES,
        label=f"transfer source {rel.as_posix()}",
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(
            f"transfer source is not UTF-8: {rel.as_posix()}"
        ) from exc
    if scan_text_for_secrets(text):
        raise BundleSecurityError(f"secret-like content detected in {rel.as_posix()}")
    bundle["files"].append(
        {
            "path": rel.as_posix(),
            "encoding": "utf-8",
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }
    )


def _add_tree(bundle: dict[str, Any], agent_root: Path, root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if _is_runtime_path(path.relative_to(agent_root)):
            continue
        _add_file(bundle, agent_root, path)


def _ensure_allowed(path: Path) -> None:
    parts = path.parts
    if not parts or parts[0] != ".agent":
        raise ValueError(f"transfer path must live under .agent/: {path}")
    if ".." in parts or path.is_absolute():
        raise ValueError(f"unsafe transfer path: {path}")
    if parts[:3] == (".agent", "protocols", "permissions.md"):
        raise ValueError("transfer may not overwrite .agent/protocols/permissions.md")
    if _is_crg_derived_path(path):
        raise ValueError(f"CRG derived graph state is not transferable: {path.as_posix()}")
    if _is_runtime_path(path):
        raise ValueError(
            f"runtime/database/cache path is not transferable: {path.as_posix()}"
        )


def _is_runtime_path(path: Path) -> bool:
    if _is_crg_derived_path(path):
        return True
    if any(part.casefold() in RUNTIME_PARTS for part in path.parts):
        return True
    return path.suffix.casefold() in RUNTIME_SUFFIXES


def _is_crg_derived_path(path: Path) -> bool:
    """Recognize CRG graph databases, registries, snapshots, and cache state.

    This intentionally does not match the portable evidence ledger: evidence
    may cite CRG provenance but is not a graph database or derived cache.
    """
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    compact_parts = tuple(re.sub(r"[^a-z0-9]", "", part) for part in parts)
    compact_stem = re.sub(r"[^a-z0-9]", "", path.stem.casefold())
    if any(part in CRG_DERIVED_PARTS for part in parts):
        return True
    if any(part in CRG_DERIVED_COMPACT_MARKERS for part in compact_parts):
        return True
    if compact_stem in CRG_DERIVED_COMPACT_MARKERS:
        return True
    if (
        compact_stem in {"crg", "graph"}
        and path.suffix.casefold() in RUNTIME_SUFFIXES
    ):
        return True
    if path.suffix.casefold() in CRG_DERIVED_SUFFIXES:
        return True
    if name.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm")):
        return True
    # Do not use substrings such as ``graph in name``: words like
    # ``paragraph`` and ordinary graph-theory prose are portable content.
    return False


def _load_accepted_lessons(agent_root: Path) -> list[dict[str, Any]]:
    jsonl = agent_root / "memory" / "semantic" / "lessons.jsonl"
    if not jsonl.is_file():
        return []
    raw = _read_safe_source_file(
        jsonl, root=agent_root, max_bytes=MAX_LEDGER_BYTES,
        label="accepted lesson ledger",
    )
    rows = _parse_jsonl_bytes(
        raw, label="accepted lesson ledger", max_bytes=MAX_LEDGER_BYTES
    )
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        if row.get("status") == "accepted":
            _validate_accepted_lesson(
                row, f"accepted lesson ledger row {index}"
            )
        lesson_id = row.get("id")
        if not isinstance(lesson_id, str) or not lesson_id:
            raise ValueError(f"accepted lesson ledger row {index} has no id")
        if lesson_id not in latest:
            order.append(lesson_id)
        latest[lesson_id] = row
    accepted = [latest[lesson_id] for lesson_id in order
                if latest[lesson_id].get("status") == "accepted"]
    if len(accepted) > MAX_LESSON_RECORDS:
        raise ValueError("accepted lesson export exceeds record bound")
    return accepted


def _version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:
        return "unknown"


def copy_agent_template(
    stack_root: Path, target_root: Path, *, target_root_fd: int | None = None
) -> None:
    """Copy trusted brain infrastructure without smuggling transfer data.

    Adapter installation still records the standard/minimal profile normally;
    this bootstrap copy only prevents a fresh transfer from inheriting the
    installer's own governance, runtime, databases, or caches. The canonical
    permissions policy is trusted stack infrastructure, not bundle data.
    """
    src = stack_root / ".agent"
    if target_root_fd is not None:
        _copy_agent_template_at(src, target_root_fd)
        return
    dst = target_root / ".agent"
    if not dst.exists() and src.is_dir():
        excluded_directories = {
            Path("memory/working"),
            Path("memory/episodic"),
            Path("memory/candidates"),
            Path("runtime"),
        }
        excluded_files = {
            Path("memory/personal/PREFERENCES.md"),
            Path("memory/semantic/DECISIONS.md"),
            Path("memory/semantic/LESSONS.md"),
            Path("memory/semantic/lessons.jsonl"),
            Path("memory/evidence/ledger.jsonl"),
        }

        def ignore(directory: str, names: list[str]) -> set[str]:
            relative = Path(directory).relative_to(src)
            omitted: set[str] = set()
            for name in names:
                candidate = relative / name
                if (
                    candidate in excluded_directories
                    or candidate in excluded_files
                    or _is_runtime_path(candidate)
                ):
                    omitted.add(name)
            return omitted

        shutil.copytree(src, dst, ignore=ignore)
        scaffolding = {
            Path("memory/personal/PREFERENCES.md"): b"# Preferences\n\n",
            Path("memory/semantic/DECISIONS.md"): b"# Decisions\n\n",
            Path("memory/semantic/lessons.jsonl"): b"",
            Path("memory/semantic/LESSONS.md"): (
                b"# Lessons\n\n"
                b"> _Auto-managed below. Hand-curated preamble + seed lessons "
                b"above the sentinel are preserved across renders._\n\n"
                + SENTINEL.encode("utf-8")
                + b"\n"
            ),
        }
        for relative, content in scaffolding.items():
            destination = dst / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)


def _template_excludes(relative: Path) -> bool:
    excluded_directories = {
        Path("memory/working"),
        Path("memory/episodic"),
        Path("memory/candidates"),
        Path("runtime"),
    }
    excluded_files = {
        Path("memory/personal/PREFERENCES.md"),
        Path("memory/semantic/DECISIONS.md"),
        Path("memory/semantic/LESSONS.md"),
        Path("memory/semantic/lessons.jsonl"),
        Path("memory/evidence/ledger.jsonl"),
    }
    return (
        relative in excluded_files
        or _is_runtime_path(relative)
        or any(directory == relative or directory in relative.parents for directory in excluded_directories)
    )


def _copy_agent_template_at(src: Path, target_root_fd: int) -> None:
    """Bootstrap trusted infrastructure through an already-pinned target FD."""
    if not src.is_dir():
        return
    try:
        os.stat(".agent", dir_fd=target_root_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.mkdir(".agent", 0o755, dir_fd=target_root_fd)
    else:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    agent_fd = os.open(".agent", flags, dir_fd=target_root_fd)
    try:
        _copy_template_tree_at(src, agent_fd, Path("."))
        scaffolding = {
            Path("memory/personal/PREFERENCES.md"): b"# Preferences\n\n",
            Path("memory/semantic/DECISIONS.md"): b"# Decisions\n\n",
            Path("memory/semantic/lessons.jsonl"): b"",
            Path("memory/semantic/LESSONS.md"): (
                b"# Lessons\n\n"
                b"> _Auto-managed below. Hand-curated preamble + seed lessons "
                b"above the sentinel are preserved across renders._\n\n"
                + SENTINEL.encode("utf-8")
                + b"\n"
            ),
        }
        for relative, content in scaffolding.items():
            _write_template_at(agent_fd, relative, content)
    finally:
        os.close(agent_fd)


def _copy_template_tree_at(source: Path, destination_fd: int, relative: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        child_relative = Path(child.name) if relative == Path(".") else (relative / child.name)
        if _template_excludes(child_relative):
            continue
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"trusted template contains symbolic link: {child_relative}")
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(child.name, stat.S_IMODE(info.st_mode), dir_fd=destination_fd)
            child_fd = os.open(child.name, flags, dir_fd=destination_fd)
            try:
                _copy_template_tree_at(child, child_fd, child_relative)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(info.st_mode):
            _write_template_name_at(
                destination_fd, child.name, child.read_bytes(), stat.S_IMODE(info.st_mode)
            )
        else:
            raise ValueError(f"trusted template contains unsafe path: {child_relative}")


def _write_template_at(destination_fd: int, relative: Path, content: bytes) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.dup(destination_fd)
    try:
        for part in relative.parent.parts:
            if part in ("", "."):
                continue
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=parent_fd)
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        _write_template_name_at(parent_fd, relative.name, content, 0o644)
    finally:
        os.close(parent_fd)


def _write_template_name_at(parent_fd: int, name: str, content: bytes, mode: int) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
