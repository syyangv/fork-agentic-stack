"""Transactional migration of installer-owned MemOS configs without startup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


_PROJECT = re.compile(r"[0-9a-f]{16}\Z")
_ROLLBACK = re.compile(r"\.([0-9a-f]{16})\.rollback-[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class ConfigSnapshot:
    path: Path
    raw: bytes
    mode: int
    mtime_ns: int
    atime_ns: int
    device: int
    inode: int
    uid: int
    gid: int
    parent_device: int
    parent_inode: int


@dataclass(frozen=True)
class MigrationResult:
    migrated: tuple[Path, ...]
    provenance: Path


def approved_config(project_id: str) -> dict:
    """Exact model-free 2.0.14 profile emitted by the reviewed runtime."""
    if _PROJECT.fullmatch(project_id) is None:
        raise ValueError("invalid project ID")
    return {
        "version": 1,
        "viewer": {"bindHost": "127.0.0.1", "openOnFirstTurn": False},
        "bridge": {"mode": "stdio"},
        "embedding": {"enabled": False, "provider": "lexical", "engine": "sqlite_fts5"},
        "llm": {"provider": "local_only", "fallbackToHost": False, "maxRetries": 0},
        "algorithm": {"lightweightMemory": {"enabled": True},
                      "capture": {"embedTraces": False}},
        "hub": {"enabled": False, "role": "client"},
        "telemetry": {"enabled": False},
        "logging": {
            "level": "info", "detailedView": False,
            "console": {"enabled": False, "pretty": False, "channels": []},
            "file": {"enabled": True, "format": "json", "retentionDays": 30},
            "llmLog": {"enabled": False, "redactPrompts": True,
                       "redactCompletions": True},
        },
    }


def _owned_configs(data_root: Path) -> tuple[Path, ...]:
    pilots = data_root / "pilot-configs"
    _real_owned_directory(data_root)
    _real_owned_directory(pilots)
    project_ids: list[str] = []
    for entry in sorted(pilots.iterdir(), key=lambda path: path.name):
        if entry.suffix != ".json":
            continue
        project_id = entry.stem
        if _PROJECT.fullmatch(project_id):
            _validate_pilot_manifest(entry, project_id)
            project_ids.append(project_id)
    configs: list[Path] = []
    for project_id in project_ids:
        active = data_root / project_id
        if active.exists():
            configs.append(active / "profiles" / project_id / "memos-plugin" / "config.yaml")
        for entry in sorted(data_root.iterdir(), key=lambda path: path.name):
            match = _ROLLBACK.fullmatch(entry.name)
            if match and match.group(1) == project_id:
                configs.append(entry / "profiles" / project_id / "memos-plugin" / "config.yaml")
    return tuple(configs)


def owned_config_paths(data_root: str | Path) -> tuple[Path, ...]:
    """Return only configs rooted in installer project manifests."""
    raw = Path(data_root).expanduser().absolute()
    _real_owned_directory(raw)
    return _owned_configs(raw)


def _real_owned_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"managed MemOS directory is missing or symlinked: {path}")
    info = path.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError(f"managed MemOS directory owner mismatch: {path}")


def _validate_pilot_manifest(path: Path, project_id: str) -> None:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"MemOS ownership manifest is not a regular file: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError(f"MemOS ownership manifest owner mismatch: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"MemOS ownership manifest mode is unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"MemOS ownership manifest is invalid: {path}") from exc
    required = {"schema", "enabled", "project_id", "repo_root", "provider", "model",
                "daily_caps", "min_distinct_episodes", "timeout_seconds"}
    if (not isinstance(value, dict) or set(value) != required
            or value.get("schema") != "agentic.memory.evolution-pilot.v2"
            or value.get("project_id") != project_id):
        raise ValueError(f"MemOS ownership manifest schema is invalid: {path}")


def _snapshot(path: Path) -> ConfigSnapshot:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError(f"managed MemOS config has symlink component: {path}")
        current = current.parent
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"managed MemOS config is not a regular file: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError(f"managed MemOS config owner mismatch: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise ValueError(f"managed MemOS config mode is unsafe: {path}")
    parent = path.parent.stat()
    if info.st_nlink != 1 or info.st_gid != parent.st_gid:
        raise ValueError(f"managed MemOS config ownership topology is unsafe: {path}")
    if getattr(info, "st_flags", 0) or (hasattr(os, "listxattr") and os.listxattr(path)):
        raise ValueError(f"managed MemOS config has unsupported metadata: {path}")
    raw = path.read_bytes()
    return ConfigSnapshot(path, raw, mode, info.st_mtime_ns, info.st_atime_ns,
                          info.st_dev, info.st_ino,
                          info.st_uid, info.st_gid, parent.st_dev, parent.st_ino)


def _replacement(snapshot: ConfigSnapshot) -> bytes | None:
    try:
        value = json.loads(snapshot.raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"managed MemOS config is invalid JSON: {snapshot.path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"managed MemOS config is not an object: {snapshot.path}")
    embedding = value.get("embedding")
    approved = {"enabled": False, "provider": "lexical", "engine": "sqlite_fts5"}
    if embedding == approved:
        return None
    if embedding not in (
        {"provider": "local", "model": "Xenova/all-MiniLM-L6-v2"},
        {"provider": "local", "model": "Xenova/all-MiniLM-L6-v2",
         "cache": {"enabled": True, "maxItems": 20000}},
    ):
        raise ValueError(f"managed MemOS config is outside legacy allowlist: {snapshot.path}")
    if (value.get("telemetry", {}).get("enabled") is not False
            or value.get("hub", {}).get("enabled") is not False
            or value.get("llm") not in (
                {"provider": "local_only", "fallbackToHost": False, "maxRetries": 0},
                {"provider": "host", "model": "opus", "fallbackToHost": False,
                 "maxRetries": 0},
            )):
        raise ValueError(f"managed MemOS legacy config violates offline policy: {snapshot.path}")
    value = approved_config(snapshot.path.parts[-3])
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _same_identity(snapshot: ConfigSnapshot) -> bool:
    try:
        info = snapshot.path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and not snapshot.path.is_symlink()
            and (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
                 stat.S_IMODE(info.st_mode), info.st_mtime_ns)
            == (snapshot.device, snapshot.inode, snapshot.uid, snapshot.gid,
                snapshot.mode, snapshot.mtime_ns)
            and hashlib.sha256(snapshot.path.read_bytes()).digest()
            == hashlib.sha256(snapshot.raw).digest())


def _atomic_write(path: Path, raw: bytes, mode: int, mtime_ns: int,
                  atime_ns: int, uid: int, gid: int,
                  parent_identity: tuple[int, int] | None = None,
                  expected_current: bytes | None = None) -> None:
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0))
    parent_info = os.fstat(parent_fd)
    if parent_identity is not None and (parent_info.st_dev, parent_info.st_ino) != parent_identity:
        os.close(parent_fd)
        raise RuntimeError(f"managed MemOS config parent changed concurrently: {path}")
    temporary = f".{path.name}.migration-{os.getpid()}-{os.urandom(8).hex()}"
    guard = f".{path.name}.guard-{os.getpid()}-{os.urandom(8).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 mode, dir_fd=parent_fd)
    try:
        os.fchmod(fd, mode)
        if hasattr(os, "fchown"):
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(temporary, ns=(atime_ns, mtime_ns), dir_fd=parent_fd,
                 follow_symlinks=False)
        if expected_current is None:
            os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent_fd)
        else:
            os.rename(path.name, guard, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            current_fd = os.open(guard, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                 dir_fd=parent_fd)
            try:
                with os.fdopen(current_fd, "rb", closefd=True) as current:
                    if current.read() != expected_current:
                        raise RuntimeError(f"managed MemOS config changed concurrently: {path}")
                os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False)
                os.unlink(temporary, dir_fd=parent_fd)
                os.unlink(guard, dir_fd=parent_fd)
            except BaseException:
                try:
                    os.link(guard, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                            follow_symlinks=False)
                    os.unlink(guard, dir_fd=parent_fd)
                except FileExistsError:
                    # Preserve the displaced file as a recovery guard rather
                    # than overwrite a concurrent writer's destination.
                    pass
                raise
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def migrate_owned_legacy_configs(data_root: str | Path, provenance_root: str | Path,
                                 *, configs: tuple[Path, ...] | None = None,
                                 fail_after: int | None = None) -> MigrationResult:
    """Atomically migrate manifest-owned configs, compensating on any failure."""
    raw_root = Path(data_root).expanduser().absolute()
    if os.name != "posix":
        raise ValueError("MemOS config migration is supported only on POSIX hosts")
    _real_owned_directory(raw_root)
    root = raw_root.resolve(strict=True)
    provenance_raw = Path(provenance_root).expanduser().absolute()
    if provenance_raw.parent.resolve(strict=True) != root:
        raise ValueError("MemOS migration provenance must be directly under data root")
    provenance = root / provenance_raw.name
    if provenance.exists() or provenance.is_symlink():
        _real_owned_directory(provenance)
    else:
        provenance.mkdir(mode=0o700)
        _real_owned_directory(provenance)
    selected = owned_config_paths(root) if configs is None else tuple(configs)
    if any(not path.is_absolute() or not path.is_relative_to(root) for path in selected):
        raise ValueError("MemOS migration config escaped managed data root")
    if configs is not None and owned_config_paths(root) != selected:
        raise RuntimeError("MemOS installer ownership set changed concurrently")
    snapshots = tuple(_snapshot(path) for path in selected)
    replacements = tuple((snap, _replacement(snap)) for snap in snapshots)
    migration_rows = [(snap, replacement) for snap, replacement in replacements
                      if replacement is not None]
    record_path = provenance / "current.json"
    record = {
        "schema": "agentic.memory.memos-config-migration.v1",
        "committed": False,
        "configs": [{
            "path": str(snap.path.relative_to(root)),
            "sha256": hashlib.sha256(snap.raw).hexdigest(),
            "mode": snap.mode,
            "mtime_ns": snap.mtime_ns,
        } for snap, replacement in migration_rows],
    }
    if not migration_rows:
        return MigrationResult((), record_path)
    record_raw = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    provenance_info = provenance.stat()
    existing_record = record_path.read_bytes() if record_path.exists() else None
    _atomic_write(record_path, record_raw, 0o600, 0, 0,
                  provenance_info.st_uid, provenance_info.st_gid,
                  (provenance_info.st_dev, provenance_info.st_ino), existing_record)
    published: list[ConfigSnapshot] = []
    try:
        for snapshot, replacement in replacements:
            if replacement is None:
                continue
            if not _same_identity(snapshot):
                raise RuntimeError(f"managed MemOS config changed concurrently: {snapshot.path}")
            _atomic_write(snapshot.path, replacement, snapshot.mode, snapshot.mtime_ns,
                          snapshot.atime_ns, snapshot.uid, snapshot.gid,
                          (snapshot.parent_device, snapshot.parent_inode), snapshot.raw)
            published.append(snapshot)
            if fail_after is not None and len(published) >= fail_after:
                raise RuntimeError("injected migration failure")
        record["committed"] = True
        committed = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
        record_info = record_path.stat()
        _atomic_write(record_path, committed, 0o600, record_info.st_mtime_ns,
                      record_info.st_atime_ns, record_info.st_uid, record_info.st_gid,
                      (provenance_info.st_dev, provenance_info.st_ino), record_raw)
        return MigrationResult(tuple(s.path for s in published), record_path)
    except BaseException:
        for snapshot in reversed(published):
            _atomic_write(snapshot.path, snapshot.raw, snapshot.mode, snapshot.mtime_ns,
                          snapshot.atime_ns, snapshot.uid, snapshot.gid,
                          (snapshot.parent_device, snapshot.parent_inode),
                          next(replacement for snap, replacement in replacements
                               if snap.path == snapshot.path))
        raise
