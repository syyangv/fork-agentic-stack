"""Quiesced, whole-project backup and rollback for MemOS runtime state.

MemOS 2.0.10 has no backup RPC.  These helpers therefore operate only while
holding the same per-project lock used by the delivery worker and preserve the
entire project root, including SQLite WAL/SHM files and the active profile.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memos_journal import _project_lock, stable_project_lock_path
from .memos_runtime import (
    _is_managed_alternate_config, build_memos_config, validate_project_id,
)


BACKUP_SCHEMA = "agentic.memory.memos-backup.v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class MemosBackupError(RuntimeError):
    """The requested snapshot or restore failed a safety invariant."""


def create_project_backup(
    project_root: str | Path,
    backup_root: str | Path,
    project_id: str,
) -> Path:
    """Create an owner-only, digest-manifested snapshot of one project root."""
    project_id = validate_project_id(project_id)
    source = _validate_project_root(project_root, project_id, must_exist=True)
    destination_root = _secure_directory(backup_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    final = destination_root / f"{project_id}-{timestamp}"
    if final.exists():  # practically impossible, but never merge snapshots
        raise MemosBackupError("backup destination already exists")

    lock_path = stable_project_lock_path(source)
    with _project_lock(lock_path):
        _validate_runtime_health(source, project_id)
        temporary = Path(tempfile.mkdtemp(prefix=f".{project_id}-", dir=destination_root))
        os.chmod(temporary, 0o700)
        try:
            payload = temporary / "project"
            _copy_tree(source, payload)
            files = _inventory(payload)
            manifest = {
                "schema": BACKUP_SCHEMA,
                "project_id": project_id,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "files": files,
            }
            _write_private_json(temporary / "manifest.json", manifest)
            _verify_snapshot(temporary, project_id)
            os.replace(temporary, final)
            _fsync(destination_root)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return final


def restore_project_backup(
    backup: str | Path,
    project_root: str | Path,
    project_id: str,
) -> Path | None:
    """Atomically replace a stopped project root and preserve its prior state.

    The returned path is an owner-only rollback copy of the state that was
    replaced, or ``None`` when the target did not previously exist.  Callers
    must cooperatively close the bridge before invoking this function.
    """
    project_id = validate_project_id(project_id)
    snapshot = Path(backup).expanduser().resolve(strict=True)
    _verify_snapshot(snapshot, project_id)
    target = _validate_project_root(project_root, project_id, must_exist=False)
    parent = _secure_directory(target.parent)
    lock_path = stable_project_lock_path(target)
    # The lock file is created as needed.  Holding it excludes compliant live
    # delivery workers; lifecycle ownership still requires the caller to have
    # closed the bridge before restore.
    with _project_lock(lock_path):
        staging = parent / f".{project_id}.restore-{uuid.uuid4().hex}"
        rollback = parent / f".{project_id}.rollback-{uuid.uuid4().hex}"
        try:
            _copy_tree(snapshot / "project", staging)
            if _inventory(staging) != _read_manifest(snapshot)["files"]:
                raise MemosBackupError("restored staging tree failed digest verification")
            _validate_runtime_health(staging, project_id)
            previous = None
            if target.exists():
                os.replace(target, rollback)
                previous = rollback
            try:
                os.replace(staging, target)
            except BaseException:
                if previous is not None and not target.exists():
                    os.replace(previous, target)
                raise
            _fsync(parent)
            return previous
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise


def _validate_project_root(
    value: str | Path, project_id: str, *, must_exist: bool,
) -> Path:
    raw = Path(value).expanduser()
    if raw.name != project_id:
        raise MemosBackupError("project root basename does not match project ID")
    _reject_symlink_components(raw, allow_missing_leaf=not must_exist)
    path = raw.resolve(strict=must_exist)
    if must_exist and not path.is_dir():
        raise MemosBackupError("project root must be a directory")
    return path


def _secure_directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    _reject_symlink_components(path, allow_missing_leaf=True)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = path.resolve(strict=True)
    if not path.is_dir() or path.is_symlink():
        raise MemosBackupError("runtime directory must be a real directory")
    os.chmod(path, 0o700)
    return path


def _reject_symlink_components(path: Path, *, allow_missing_leaf: bool) -> None:
    absolute = path.absolute()
    parts = absolute.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], 1):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if allow_missing_leaf:
                return
            raise MemosBackupError("runtime path does not exist") from None
        if stat.S_ISLNK(mode):
            raise MemosBackupError("symlink components are not allowed")


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        raise MemosBackupError("copy destination already exists")
    for path in [source, *source.rglob("*")]:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise MemosBackupError("symlinks are not allowed in MemOS runtime state")
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise MemosBackupError("special files are not allowed in MemOS runtime state")
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
    for path in [destination, *destination.rglob("*")]:
        if path.is_dir():
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o600)


def _inventory(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            rows.append({"path": relative, "type": "directory"})
        elif stat.S_ISREG(mode):
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            rows.append({
                "path": relative, "type": "file", "bytes": size,
                "sha256": digest.hexdigest(),
            })
        else:
            raise MemosBackupError("snapshot contains a special file")
    return rows


def _read_manifest(snapshot: Path) -> dict[str, Any]:
    try:
        raw = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemosBackupError("backup manifest is unreadable") from exc
    if not isinstance(raw, dict) or raw.get("schema") != BACKUP_SCHEMA:
        raise MemosBackupError("backup manifest schema is invalid")
    return raw


def _verify_snapshot(snapshot: Path, project_id: str) -> None:
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise MemosBackupError("backup must be a real directory")
    manifest = _read_manifest(snapshot)
    if manifest.get("project_id") != project_id:
        raise MemosBackupError("backup project ID does not match target")
    expected = manifest.get("files")
    if not isinstance(expected, list):
        raise MemosBackupError("backup file inventory is invalid")
    for row in expected:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise MemosBackupError("backup inventory row is invalid")
        if Path(row["path"]).is_absolute() or ".." in Path(row["path"]).parts:
            raise MemosBackupError("backup inventory path is unsafe")
        if row.get("type") == "file" and _DIGEST.fullmatch(str(row.get("sha256"))) is None:
            raise MemosBackupError("backup digest is invalid")
    if _inventory(snapshot / "project") != expected:
        raise MemosBackupError("backup contents do not match manifest")


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _validate_runtime_health(root: Path, project_id: str) -> None:
    """Validate the managed profile and every runtime SQLite store read-only."""
    config_path = root / "profiles" / project_id / "memos-plugin" / "config.yaml"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemosBackupError("runtime managed config is invalid") from exc
    if config != build_memos_config(project_id) and not _is_managed_alternate_config(
        config, project_id,
    ):
        raise MemosBackupError("runtime managed config is not recognized")
    databases = sorted({
        path for pattern in ("*.sqlite3", "*.db") for path in root.rglob(pattern)
        if path.is_file()
    })
    for database in databases:
        try:
            uri = database.resolve(strict=True).as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as connection:
                row = connection.execute("pragma quick_check").fetchone()
        except sqlite3.Error as exc:
            raise MemosBackupError("runtime database health check failed") from exc
        if row != ("ok",):
            raise MemosBackupError("runtime database health check failed")


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "BACKUP_SCHEMA", "MemosBackupError", "create_project_backup",
    "restore_project_backup",
]
