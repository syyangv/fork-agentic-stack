"""Explicit, non-authoritative export of one bounded MemOS behavioral store.

This module deliberately has no import path into an exported project.  It
inspects only data files through SQLite's read-only/backup APIs and writes a
new artifact selected by an explicit caller; it neither starts nor stops a
provider and has no import or activation operation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .transfer_bundle import scan_text_for_secrets
from .memos_install import MEMOS_PLUGIN_VERSION, validate_installed_plugin

try:  # The supported runtime is POSIX; fail closed where flock is absent.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


BEHAVIORAL_EXPORT_SCHEMA = "agentic.memory.behavioral-export.v1"
PROFILE_SCHEMA_VERSION = 1
MAX_SKILL_FILES = 1_000
MAX_SKILL_BYTES = 1 * 1024 * 1024
MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_SQLITE_ROWS = 100_000
MAX_SQLITE_VALUE_BYTES = 1 * 1024 * 1024
_PROJECT_ID = re.compile(r"[0-9a-f]{16}\Z")
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EXTRA_SECRET = re.compile(
    r'''(?ix)(?:
        authorization\s*:\s*bearer
        |(?:token|secret|password|passphrase|api[ _-]?key|access[ _-]?token
           |client[ _-]?secret|private[ _-]?key)\s*["']?\s*(?::|=)\s*["']?\S+
    )'''
)
_RAW_SECRET = re.compile(
    rb"(?:authorization\s*:\s*bearer|(?:token|secret|password|passphrase|api[ _-]?key|access[ _-]?token|client[ _-]?secret|private[ _-]?key)\s*[\"']?\s*(?::|=)\s*[\"']?\S+|-----begin\s+[a-z ]*private\s+key-----|(?:sk|rk|pk)-(?:proj-)?[a-z0-9_-]{12,})",
    re.I,
)


class BehavioralExportError(ValueError):
    """A behavioral export violated its non-authoritative safety contract."""


@dataclass(frozen=True)
class _SkillEntry:
    relative: Path
    dev: int
    ino: int
    size: int


def export_behavioral_artifact(
    project_root: Path | str,
    destination: Path | str,
    project_id: str,
    *,
    provenance: str,
    repo_root: Path | str | None = None,
    code_root: Path | str | None = None,
) -> Path:
    """Create one digest-manifested data-only behavioral export.

    ``destination`` is the final artifact directory and must not exist.  All
    inspection, including the complete SQLite and skill secret scan, finishes
    before this final directory is created.
    """
    project_id = _validate_project_id(project_id)
    source = _validate_project_root(project_root, project_id)
    source_namespace = _namespace_identity(source)
    provenance = _validate_provenance(provenance)
    plugin = source / "profiles" / project_id / "memos-plugin"
    database = plugin / "data" / "memos.db"
    skills = plugin / "skills"
    code_root = Path(code_root) if code_root is not None else _derive_code_root(source)
    output = _validate_destination(Path(destination), source, code_root)
    lock_path = _lifecycle_lock_path(source)
    with _lifecycle_lock(lock_path):
        _assert_namespace_current(source, source_namespace)
        _reject_live_provider(source)
        _validate_managed_profile(plugin)
        _assert_namespace_current(source, source_namespace)
        try:
            validate_installed_plugin(code_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise BehavioralExportError("pinned MemOS plugin validation failed") from exc
        _validate_safe_directory(plugin / "data", "behavioral database directory")
        _validate_regular_file(database, "behavioral database", MAX_DATABASE_BYTES)
        database_fd, database_identity = _open_regular_no_follow(
            database, "behavioral database", MAX_DATABASE_BYTES,
        )
        temporary_name = f".{project_id}.behavioral-{uuid.uuid4().hex}"
        parent_fd = -1
        temporary_fd = -1
        temporary_identity: tuple[int, int] | None = None
        try:
            skill_entries = _collect_skills(skills)
            _assert_namespace_current(source, source_namespace)
            parent_fd, parent_identity = _open_output_parent(output.parent)
            os.mkdir(temporary_name, 0o700, dir_fd=parent_fd)
            created = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
            temporary_identity = (created.st_dev, created.st_ino)
            temporary_fd = os.open(
                temporary_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            if _fd_identity(temporary_fd) != temporary_identity:
                raise BehavioralExportError("behavioral staging directory changed during export")
        except BaseException:
            if parent_fd >= 0 and temporary_identity is not None:
                _remove_tree_at_if_identity(parent_fd, temporary_name, temporary_identity)
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if parent_fd >= 0:
                os.close(parent_fd)
            os.close(database_fd)
            raise
        assert temporary_identity is not None
        temporary = _fd_path(temporary_fd)
        try:
            _assert_output_parent_current(output.parent, parent_identity)
            _assert_namespace_current(source, source_namespace)
            _assert_temporary_current(parent_fd, temporary_name, temporary_identity)
            _mkdir_at(temporary_fd, Path("data"))
            db_raw_before = _sqlite_backup(_fd_path(database_fd))
            if len(db_raw_before) > MAX_DATABASE_BYTES:
                raise BehavioralExportError("behavioral SQLite snapshot exceeds size bound")
            _scan_sqlite_raw_bytes(db_raw_before)
            _write_private_bytes_at(temporary_fd, Path("data/memos.db"), db_raw_before)
            _assert_temporary_current(parent_fd, temporary_name, temporary_identity)
            _assert_regular_fd_current(database_fd, database_identity, "behavioral database")
            staged_db_fd, _staged_db_identity = _open_regular_at(
                temporary_fd, Path("data/memos.db"), "behavioral SQLite snapshot", MAX_DATABASE_BYTES,
            )
            try:
                _scan_sqlite(_fd_path(staged_db_fd))
            finally:
                os.close(staged_db_fd)
            db_raw = _read_regular_bytes_at(temporary_fd, Path("data/memos.db"), "behavioral SQLite snapshot", MAX_DATABASE_BYTES)
            if hashlib.sha256(db_raw_before).digest() != hashlib.sha256(db_raw).digest():
                raise BehavioralExportError("behavioral SQLite snapshot changed during inspection")
            _scan_sqlite_raw_bytes(db_raw)
            staged_bytes: dict[Path, bytes] = {Path("data/memos.db"): db_raw}
            artifacts: list[dict[str, Any]] = [_artifact_row_bytes(db_raw, Path("data/memos.db"))]
            skills_fd = _open_directory_no_follow(skills)
            try:
                for entry in skill_entries:
                    _assert_output_parent_current(output.parent, parent_identity)
                    _assert_temporary_current(parent_fd, temporary_name, temporary_identity)
                    raw = _read_skill_exact(skills_fd, entry)
                    _scan_skill_bytes(raw)
                    relative = Path("skills") / entry.relative
                    _write_private_bytes_at(temporary_fd, relative, raw)
                    # Scan the exact staged bytes immediately before manifesting.
                    staged = _read_regular_bytes_at(temporary_fd, relative, "behavioral staged skill", MAX_SKILL_BYTES)
                    if staged != raw:
                        raise BehavioralExportError("behavioral skill changed during staging")
                    _scan_skill_bytes(staged)
                    staged_bytes[relative] = staged
                    artifacts.append(_artifact_row_bytes(staged, relative))
            finally:
                os.close(skills_fd)
            artifacts.sort(key=lambda row: row["path"])
            if len(artifacts) > MAX_SKILL_FILES + 1:
                raise BehavioralExportError("behavioral artifact count exceeds bound")
            total = sum(int(row["bytes"]) for row in artifacts)
            if total > MAX_TOTAL_BYTES:
                raise BehavioralExportError("behavioral export exceeds total size bound")
            revision = _repository_revision(Path(repo_root) if repo_root is not None else source)
            authoritative = {
                "schema": BEHAVIORAL_EXPORT_SCHEMA,
                "project_id": project_id,
                "provenance": provenance,
                "repository_revision": revision,
                "memos_plugin_version": MEMOS_PLUGIN_VERSION,
                "profile_schema_version": PROFILE_SCHEMA_VERSION,
                "artifacts": artifacts,
            }
            content_digest = _canonical_digest(authoritative)
            manifest = dict(authoritative)
            manifest["created_at"] = _now_iso()
            manifest["content_digest"] = content_digest
            _assert_output_parent_current(output.parent, parent_identity)
            _write_private_json(temporary / "manifest.json", manifest)
            _verify_staged_bytes(temporary_fd, staged_bytes)
            _assert_temporary_current(parent_fd, temporary_name, temporary_identity)
            _assert_output_parent_current(output.parent, parent_identity)
            _publish_staging_no_replace(
                parent_fd, temporary_fd, temporary_name, temporary_identity, output.name,
                has_skills=bool(skill_entries),
            )
            return output
        except BaseException:
            _remove_tree_at_if_identity(parent_fd, temporary_name, temporary_identity)
            raise
        finally:
            os.close(database_fd)
            os.close(temporary_fd)
            os.close(parent_fd)


def _validate_project_id(value: str) -> str:
    if not isinstance(value, str) or _PROJECT_ID.fullmatch(value) is None:
        raise BehavioralExportError("behavioral export project ID is invalid")
    return value


def _validate_project_root(value: Path | str, project_id: str) -> Path:
    path = Path(value).expanduser()
    if path.name != project_id:
        raise BehavioralExportError("behavioral export project ID does not match project root")
    _reject_symlink_components(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BehavioralExportError("behavioral project root is unavailable") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise BehavioralExportError("behavioral project root must be a real directory")
    _assert_owner_safe(resolved, "behavioral project root")
    return resolved


def _namespace_identity(project: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    parent = project.parent.lstat()
    current = project.lstat()
    return (parent.st_dev, parent.st_ino), (current.st_dev, current.st_ino)


def _assert_namespace_current(
    project: Path, identity: tuple[tuple[int, int], tuple[int, int]],
) -> None:
    try:
        observed = _namespace_identity(project)
    except OSError as exc:
        raise BehavioralExportError("behavioral project namespace changed during export") from exc
    if observed != identity:
        raise BehavioralExportError("behavioral project namespace changed during export")


def _validate_destination(destination: Path, project_root: Path, code_root: Path) -> Path:
    if destination.exists() or destination.is_symlink():
        raise BehavioralExportError("behavioral export destination already exists")
    if destination.name in ("", ".", ".."):
        raise BehavioralExportError("behavioral export destination is invalid")
    _reject_symlink_components(destination.parent)
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise BehavioralExportError("behavioral export destination parent must exist") from exc
    _assert_secure_directory(parent)
    output = parent / destination.name
    if output.is_relative_to(project_root) or output.is_relative_to(code_root.resolve(strict=False)):
        raise BehavioralExportError("behavioral export destination is inside behavioral runtime or pinned code")
    return output


def _validate_provenance(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
        raise BehavioralExportError("behavioral export provenance is required and bounded")
    if scan_text_for_secrets(value) or _EXTRA_SECRET.search(value):
        raise BehavioralExportError("secret-like content detected in behavioral provenance")
    return value.strip()


def _validate_managed_profile(plugin: Path) -> None:
    _reject_symlink_components(plugin)
    _validate_safe_directory(plugin, "managed MemOS profile")
    config = plugin / "config.yaml"
    _validate_regular_file(config, "managed MemOS config", 64 * 1024)
    try:
        raw = _read_regular_bytes_no_follow(config, "managed MemOS config", 64 * 1024).decode("utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BehavioralExportError("managed MemOS config is invalid") from exc
    if scan_text_for_secrets(raw):
        raise BehavioralExportError("secret-like content detected in managed MemOS config")
    if not isinstance(value, dict):
        raise BehavioralExportError("managed MemOS profile schema is unsupported")
    # Every nested object is untrusted runtime data.  Check its shape before
    # reading fields so malformed JSON remains a normal, content-free policy
    # failure rather than an AttributeError escaping through the CLI.
    version = value.get("version")
    if type(version) is not int or version != PROFILE_SCHEMA_VERSION:
        raise BehavioralExportError("managed MemOS profile schema is unsupported")
    telemetry = value.get("telemetry")
    if not isinstance(telemetry, dict) or telemetry.get("enabled") is not False:
        raise BehavioralExportError("managed MemOS profile is not privacy-safe")
    bridge = value.get("bridge")
    if not isinstance(bridge, dict) or bridge.get("mode") != "stdio":
        raise BehavioralExportError("managed MemOS profile is not recognized")
    viewer = value.get("viewer")
    if (not isinstance(viewer, dict)
            or viewer.get("bindHost") != "127.0.0.1"
            or viewer.get("openOnFirstTurn") is not False):
        raise BehavioralExportError("managed MemOS profile viewer is not loopback-only")
    embedding = value.get("embedding")
    if not isinstance(embedding, dict):
        raise BehavioralExportError("managed MemOS profile embedding is not local-private")
    if (set(embedding) != {"enabled", "provider", "engine"}
            or embedding.get("enabled") is not False
            or embedding.get("provider") != "lexical"
            or embedding.get("engine") != "sqlite_fts5"):
        raise BehavioralExportError("managed MemOS profile embedding is not model-free lexical")
    llm = value.get("llm")
    if (not isinstance(llm, dict)
            or set(llm) - {"provider", "fallbackToHost", "maxRetries"}
            or llm.get("provider") != "local_only"
            or llm.get("fallbackToHost") is not False
            or "model" in llm):
        raise BehavioralExportError("managed MemOS profile is not local-only")
    hub = value.get("hub")
    if not isinstance(hub, dict) or hub.get("enabled") is not False:
        raise BehavioralExportError("managed MemOS profile hub is not disabled")
    logging = value.get("logging")
    if not isinstance(logging, dict):
        raise BehavioralExportError("managed MemOS profile logging is not redacted")
    llm_log = logging.get("llmLog")
    if not isinstance(llm_log, dict) or llm_log.get("enabled") is not False or llm_log.get("redactPrompts") is not True or llm_log.get("redactCompletions") is not True:
        raise BehavioralExportError("managed MemOS profile logging is not redacted")
    # Phase 8 must stay blocked: the export is permitted only for the normal
    # non-evolution profile and does not infer an active provider mode.
    algorithm = value.get("algorithm")
    if not isinstance(algorithm, dict):
        raise BehavioralExportError("managed MemOS profile is not the blocked-quality profile")
    lightweight_memory = algorithm.get("lightweightMemory")
    if not isinstance(lightweight_memory, dict) or lightweight_memory.get("enabled") is not True:
        raise BehavioralExportError("managed MemOS profile is not the blocked-quality profile")
    capture = algorithm.get("capture")
    if capture != {"embedTraces": False}:
        raise BehavioralExportError("managed MemOS profile does not disable trace embeddings")
    if any(key in algorithm for key in ("reward", "l2Induction", "l3Abstraction", "skill", "feedback", "retrieval")):
        raise BehavioralExportError("managed MemOS profile contains evolution fields")


def _collect_skills(root: Path) -> list[_SkillEntry]:
    _reject_symlink_components(root)
    _validate_safe_directory(root, "behavioral skills directory")
    entries: list[_SkillEntry] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        _validate_relative(relative)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            _assert_owner_safe(path, "behavioral skills directory")
            continue
        if stat.S_ISLNK(info.st_mode):
            raise BehavioralExportError("behavioral skills contain a symbolic link")
        if not stat.S_ISREG(info.st_mode):
            raise BehavioralExportError("behavioral skills contain a non-regular file")
        _assert_owner_safe(path, "behavioral skill")
        if info.st_size > MAX_SKILL_BYTES:
            raise BehavioralExportError("behavioral skill exceeds size bound")
        total += info.st_size
        if total > MAX_TOTAL_BYTES - MAX_DATABASE_BYTES:
            raise BehavioralExportError("behavioral skill total size bound exceeded")
        entries.append(_SkillEntry(relative, info.st_dev, info.st_ino, info.st_size))
        if len(entries) > MAX_SKILL_FILES:
            raise BehavioralExportError("behavioral skill count exceeds bound")
    return entries


def _validate_relative(relative: Path) -> None:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise BehavioralExportError("behavioral artifact path is unsafe")
    if any(_SAFE_COMPONENT.fullmatch(part) is None for part in relative.parts):
        raise BehavioralExportError("behavioral artifact path is unsafe")


def _scan_skill_bytes(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise BehavioralExportError("behavioral skill must be valid UTF-8 text") from exc
    if scan_text_for_secrets(text) or _EXTRA_SECRET.search(text):
        raise BehavioralExportError("secret-like content detected in behavioral export")
    if _contains_raw_secret(raw):
        raise BehavioralExportError("secret-like content detected in behavioral export")


def _scan_sqlite(path: Path) -> None:
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
    except (OSError, sqlite3.Error) as exc:
        raise BehavioralExportError("behavioral SQLite database is unavailable") from exc
    try:
        if connection.execute("pragma quick_check").fetchone() != ("ok",):
            raise BehavioralExportError("behavioral SQLite integrity check failed")
        metadata = list(connection.execute(
            "select type, name, tbl_name, sql from sqlite_master order by type, name"
        ))
        for metadata_row in metadata:
            for value in metadata_row:
                _scan_sqlite_value(value)
        tables = [row[0] for row in connection.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        )]
        rows = 0
        for table in tables:
            if not isinstance(table, str) or _SAFE_COMPONENT.fullmatch(table) is None:
                raise BehavioralExportError("behavioral SQLite schema is unsafe")
            for row in connection.execute(f'select * from "{table}"'):
                rows += 1
                if rows > MAX_SQLITE_ROWS:
                    raise BehavioralExportError("behavioral SQLite row count exceeds bound")
                for value in row:
                    _scan_sqlite_value(value)
    except sqlite3.Error as exc:
        raise BehavioralExportError("behavioral SQLite inspection failed") from exc
    finally:
        connection.close()


def _scan_sqlite_value(value: object) -> None:
    if value is None or isinstance(value, (int, float)):
        return
    if isinstance(value, str):
        raw = value.encode("utf-8")
        text = value
    elif isinstance(value, bytes):
        raw = value
        text = raw.decode("utf-8", errors="ignore")
    else:
        raise BehavioralExportError("behavioral SQLite value type is unsupported")
    if len(raw) > MAX_SQLITE_VALUE_BYTES:
        raise BehavioralExportError("behavioral SQLite value exceeds size bound")
    if scan_text_for_secrets(text) or _EXTRA_SECRET.search(text) or _contains_raw_secret(raw):
        raise BehavioralExportError("secret-like content detected in behavioral export")


def _scan_sqlite_raw_bytes(raw: bytes) -> None:
    """Scan the exact copied database image, including freelist pages."""
    if (_contains_raw_secret(raw)
            or scan_text_for_secrets(raw.decode("utf-8", errors="ignore"))
            or _EXTRA_SECRET.search(raw.decode("utf-8", errors="ignore"))):
        raise BehavioralExportError("secret-like content detected in behavioral export")


def _sqlite_backup(source: Path) -> bytes:
    try:
        # Source may be /dev/fd/<n>, which pins the checked inode across a
        # rename/symlink race.  Do not resolve it back to a mutable pathname.
        source_uri = source.as_uri() + "?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=1) as incoming:
            with sqlite3.connect(":memory:") as outgoing:
                incoming.backup(outgoing)
                return outgoing.serialize()
    except sqlite3.Error as exc:
        raise BehavioralExportError("behavioral SQLite snapshot failed") from exc


def _artifact_row_bytes(raw: bytes, relative: Path) -> dict[str, Any]:
    _validate_relative(relative)
    return {
        "path": relative.as_posix(), "type": "file", "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _write_private_bytes_at(root_fd: int, relative: Path, raw: bytes) -> None:
    _validate_relative(relative)
    directory = _open_relative_directory(root_fd, relative.parent, create=True)
    try:
        descriptor = os.open(
            relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=directory,
        )
        try:
            _write_all(descriptor, raw)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BehavioralExportError("behavioral staged skill could not be written safely") from exc
    finally:
        os.close(directory)


def _open_directory_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise BehavioralExportError("behavioral skill directory changed during export") from exc


def _read_skill_exact(skills_fd: int, entry: _SkillEntry) -> bytes:
    directory = os.dup(skills_fd)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in entry.relative.parent.parts:
            if part in ("", "."):
                continue
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        before = os.stat(entry.relative.name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino, before.st_size) != (entry.dev, entry.ino, entry.size)):
            raise BehavioralExportError("behavioral skill changed during export")
        descriptor = os.open(
            entry.relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (entry.dev, entry.ino, entry.size):
                raise BehavioralExportError("behavioral skill changed during export")
            raw = _read_exact(descriptor, entry.size)
            after = os.fstat(descriptor)
            if (after.st_dev, after.st_ino, after.st_size) != (entry.dev, entry.ino, entry.size):
                raise BehavioralExportError("behavioral skill changed during export")
            return raw
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BehavioralExportError("behavioral skill changed during export") from exc
    finally:
        os.close(directory)


def _read_exact(descriptor: int, size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            raise BehavioralExportError("behavioral skill changed during export")
        parts.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise BehavioralExportError("behavioral skill changed during export")
    return b"".join(parts)


def _fd_identity(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


def _fd_path(descriptor: int) -> Path:
    return Path(f"/dev/fd/{descriptor}")


def _open_regular_no_follow(path: Path, label: str, maximum: int) -> tuple[int, tuple[int, int, int]]:
    _validate_regular_file(path, label, maximum)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise BehavioralExportError(f"{label} changed during export") from exc
    info = os.fstat(descriptor)
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise BehavioralExportError(f"{label} changed during export")
        _assert_owned_mode(info, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, (info.st_dev, info.st_ino, info.st_size)


def _assert_regular_fd_current(descriptor: int, identity: tuple[int, int, int], label: str) -> None:
    info = os.fstat(descriptor)
    if (info.st_dev, info.st_ino, info.st_size) != identity:
        raise BehavioralExportError(f"{label} changed during export")


def _read_regular_bytes_no_follow(path: Path, label: str, maximum: int) -> bytes:
    descriptor, identity = _open_regular_no_follow(path, label, maximum)
    try:
        raw = _read_exact(descriptor, identity[2])
        _assert_regular_fd_current(descriptor, identity, label)
        return raw
    finally:
        os.close(descriptor)


def _open_relative_directory(root_fd: int, relative: Path, *, create: bool = False) -> int:
    directory = os.dup(root_fd)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in relative.parts:
            if part in ("", "."):
                continue
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except OSError as exc:
        os.close(directory)
        raise BehavioralExportError("behavioral staging directory changed during export") from exc


def _mkdir_at(root_fd: int, relative: Path) -> None:
    directory = _open_relative_directory(root_fd, relative, create=True)
    os.close(directory)


def _read_regular_bytes_at(root_fd: int, relative: Path, label: str, maximum: int) -> bytes:
    directory = _open_relative_directory(root_fd, relative.parent)
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise BehavioralExportError(f"{label} changed during export")
            _assert_owned_mode(info, label)
            return _read_exact(descriptor, info.st_size)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BehavioralExportError(f"{label} changed during export") from exc
    finally:
        os.close(directory)


def _open_regular_at(
    root_fd: int, relative: Path, label: str, maximum: int,
) -> tuple[int, tuple[int, int, int]]:
    directory = _open_relative_directory(root_fd, relative.parent)
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
    except OSError as exc:
        raise BehavioralExportError(f"{label} changed during export") from exc
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise BehavioralExportError(f"{label} changed during export")
        _assert_owned_mode(info, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, (info.st_dev, info.st_ino, info.st_size)


def _write_all(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if written <= 0:
            raise BehavioralExportError("behavioral staged skill could not be written safely")
        offset += written


def _verify_staged_bytes(root_fd: int, expected: dict[Path, bytes]) -> None:
    for relative, raw in expected.items():
        maximum = MAX_DATABASE_BYTES if relative == Path("data/memos.db") else MAX_SKILL_BYTES
        if _read_regular_bytes_at(root_fd, relative, "behavioral staged artifact", maximum) != raw:
            raise BehavioralExportError("behavioral staged artifact changed during export")


def _assert_temporary_current(parent_fd: int, name: str, identity: tuple[int, int]) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise BehavioralExportError("behavioral staging directory changed during export") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or (info.st_dev, info.st_ino) != identity):
        raise BehavioralExportError("behavioral staging directory changed during export")


def _remove_tree_at_if_identity(parent_fd: int, name: str, identity: tuple[int, int]) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
        _remove_tree_at(parent_fd, name)


def _publish_staging_no_replace(
    parent_fd: int, temporary_fd: int, temporary_name: str,
    temporary_identity: tuple[int, int], output_name: str, *, has_skills: bool,
) -> None:
    _assert_temporary_current(parent_fd, temporary_name, temporary_identity)
    try:
        os.mkdir(output_name, 0o700, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise BehavioralExportError("behavioral export destination already exists") from exc
    destination_fd = -1
    destination_identity: tuple[int, int] | None = None
    try:
        destination_fd = os.open(
            output_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        destination_identity = _fd_identity(destination_fd)
        _assert_temporary_current(parent_fd, temporary_name, temporary_identity)
        # Manifest is moved last.  A crash can leave only an explicitly
        # incomplete, manifest-less reservation; normal failure cleanup removes
        # it identity-safely and consumers must require the manifest.
        children = ("data", "skills", "manifest.json") if has_skills else ("data", "manifest.json")
        for child in children:
            os.rename(child, child, src_dir_fd=temporary_fd, dst_dir_fd=destination_fd)
        _assert_temporary_current(parent_fd, temporary_name, temporary_identity)
        os.rmdir(temporary_name, dir_fd=parent_fd)
    except BaseException:
        if destination_identity is not None:
            _remove_tree_at_if_identity(parent_fd, output_name, destination_identity)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)


def _contains_raw_secret(raw: bytes) -> bool:
    # UTF-16 and NUL-separated textual BLOBs are normalized for signatures;
    # no decoded secret is ever included in an exception or manifest.
    compact = raw.replace(b"\x00", b"")
    return _RAW_SECRET.search(raw) is not None or _RAW_SECRET.search(compact) is not None


def _reject_live_provider(project: Path) -> None:
    attestation = project / "bridge-process.json"
    if not attestation.exists() and not attestation.is_symlink():
        return
    try:
        raw = _read_regular_bytes_no_follow(
            attestation, "MemOS bridge attestation", 16 * 1024,
        )
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BehavioralExportError("MemOS bridge attestation is invalid") from exc
    pid = record.get("pid") if isinstance(record, dict) else None
    if type(pid) is not int or pid <= 0:
        raise BehavioralExportError("MemOS bridge attestation is invalid")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise BehavioralExportError("MemOS bridge liveness cannot be determined") from exc
    raise BehavioralExportError("behavioral export refused while compliant MemOS provider is live")


def _lifecycle_lock_path(project: Path) -> Path:
    return project.parent / f".{project.name}.memos-lifecycle.lock"


def _derive_code_root(project: Path) -> Path:
    # Normal runtime is <agent>/runtime/memos/<project>; isolated fixtures may
    # place <project> directly below a data root.
    runtime = project.parent.parent if project.parent.name == "memos" else project.parent
    return runtime / "providers"


@contextmanager
def _lifecycle_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise BehavioralExportError("behavioral export lifecycle lock is unavailable")
    # The runtime owns this stable lock for the project's lifetime.  Export
    # must never create/unlink it: unlinking after a lock release creates an
    # ABA split-lock race for other waiters that still hold the old inode.
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    locked = False
    try:
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise BehavioralExportError("behavioral export lifecycle lock is unavailable") from exc
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BehavioralExportError("behavioral export lifecycle lock is unsafe")
        _assert_owned_mode(info, "behavioral export lifecycle lock")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BehavioralExportError("behavioral export refused: MemOS lifecycle lock is live") from exc
        locked = True
        yield
    finally:
        if descriptor >= 0:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _validate_regular_file(path: Path, label: str, maximum: int) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BehavioralExportError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BehavioralExportError(f"{label} must not be symbolic")
    if not stat.S_ISREG(info.st_mode):
        raise BehavioralExportError(f"{label} must be a regular file")
    _assert_owned_mode(info, label)
    if info.st_size > maximum:
        raise BehavioralExportError(f"{label} exceeds size bound")


def _validate_safe_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BehavioralExportError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BehavioralExportError(f"{label} must not be symbolic")
    if not stat.S_ISDIR(info.st_mode):
        raise BehavioralExportError(f"{label} must be a real directory")
    _assert_owned_mode(info, label)


def _assert_owner_safe(path: Path, label: str) -> None:
    _assert_owned_mode(path.lstat(), label)


def _assert_owned_mode(info: os.stat_result, label: str) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise BehavioralExportError(f"{label} ownership is unsafe")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise BehavioralExportError(f"{label} permissions are unsafe")


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise BehavioralExportError("behavioral export path contains a symbolic link")


def _assert_secure_directory(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise BehavioralExportError("behavioral export destination parent is unsafe")
    _assert_owner_safe(path, "behavioral export destination parent")


def _open_output_parent(parent: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as exc:
        raise BehavioralExportError("behavioral export destination parent is unsafe") from exc
    info = os.fstat(descriptor)
    try:
        if not stat.S_ISDIR(info.st_mode):
            raise BehavioralExportError("behavioral export destination parent is unsafe")
        _assert_owned_mode(info, "behavioral export destination parent")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, (info.st_dev, info.st_ino)


def _assert_output_parent_current(parent: Path, identity: tuple[int, int]) -> None:
    try:
        info = parent.lstat()
    except OSError as exc:
        raise BehavioralExportError("behavioral export destination parent changed") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        raise BehavioralExportError("behavioral export destination parent changed")


def _remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        for child in os.listdir(descriptor):
            _remove_tree_at(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


def _repository_revision(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        revision = completed.stdout.strip()
        return revision if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", revision) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _canonical_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    parts = path.parts
    if len(parts) >= 5 and parts[:3] == ("/", "dev", "fd") and parts[3].isdigit():
        _write_private_bytes_at(int(parts[3]), Path(*parts[4:]), raw)
        return
    # Only retained for trusted local callers outside export staging.
    path.write_bytes(raw)
    os.chmod(path, 0o600)


__all__ = ["BEHAVIORAL_EXPORT_SCHEMA", "BehavioralExportError", "export_behavioral_artifact"]
