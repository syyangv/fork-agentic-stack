"""Strict, non-secret local configuration for scheduled review presentation.

This module only validates/copies data.  It does not start services, inspect
processes, or activate launchd; those lifecycle actions remain outside Gate 11.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


DEFAULT_LOCAL_CONFIG = Path("memory/orchestration/scheduled-local.json")
DEFAULT_LOCAL_TEMPLATE = Path("memory/orchestration/scheduled-local.default.json")
SCHEMA = "agentic.memory.scheduled-local.v1"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 48999
_NOTIFICATIONS = frozenset({"disabled", "requested"})
_KEYS = frozenset({
    "schema", "obsidian_path", "notification", "maintenance_schedule",
    "review_schedule", "review_server_host", "review_server_port",
})
MAX_LOCAL_CONFIG_BYTES = 16 * 1024
_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _schedule(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"hour", "minute"}:
        raise ValueError(f"{name} must contain only hour and minute")
    hour, minute = value["hour"], value["minute"]
    if type(hour) is not int or type(minute) is not int or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{name} must be a bounded local time")
    return {"hour": hour, "minute": minute}


@dataclass(frozen=True)
class LocalScheduleConfig:
    schema: str
    obsidian_path: Optional[str]
    notification: str
    maintenance_schedule: dict[str, int]
    review_schedule: dict[str, int]
    review_server_host: str
    review_server_port: int

    @classmethod
    def from_external(cls, value: object) -> "LocalScheduleConfig":
        if not isinstance(value, Mapping) or set(value) != _KEYS:
            raise ValueError("scheduled local config must contain exactly the versioned data fields")
        schema = value["schema"]
        obsidian_path = value["obsidian_path"]
        notification = value["notification"]
        host = value["review_server_host"]
        port = value["review_server_port"]
        if schema != SCHEMA:
            raise ValueError("scheduled local config schema is unsupported")
        if obsidian_path is not None and (
            not isinstance(obsidian_path, str) or not obsidian_path or "\x00" in obsidian_path
            or len(obsidian_path) > 4096
            or any(ord(character) < 32 for character in obsidian_path)
            or not Path(obsidian_path).is_absolute()
        ):
            raise ValueError("obsidian_path must be an optional absolute non-secret path")
        if notification not in _NOTIFICATIONS:
            raise ValueError("notification must be an explicit supported preference")
        if host != LOOPBACK_HOST:
            raise ValueError("review_server_host must be loopback-only")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("review_server_port must be a bounded non-privileged port")
        return cls(
            schema=SCHEMA,
            obsidian_path=obsidian_path,
            notification=notification,
            maintenance_schedule=_schedule(value["maintenance_schedule"], "maintenance_schedule"),
            review_schedule=_schedule(value["review_schedule"], "review_schedule"),
            review_server_host=LOOPBACK_HOST,
            review_server_port=port,
        )


def load_local_schedule_config(path: str | Path) -> LocalScheduleConfig:
    """Read a bounded JSON document; absent configuration falls back safely."""
    config_path = Path(path)
    descriptor = -1
    try:
        descriptor = _open_file_nofollow(config_path)
    except FileNotFoundError:
        return LocalScheduleConfig(
            schema=SCHEMA, obsidian_path=None, notification="disabled",
            maintenance_schedule={"hour": 3, "minute": 0},
            review_schedule={"hour": 9, "minute": 0},
            review_server_host=LOOPBACK_HOST, review_server_port=DEFAULT_PORT,
        )
    except OSError as exc:
        raise ValueError("scheduled local config is invalid") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("scheduled local config is not a regular file")
        raw = _read_all(descriptor, MAX_LOCAL_CONFIG_BYTES + 1)
        if len(raw) > MAX_LOCAL_CONFIG_BYTES:
            raise ValueError("scheduled local config exceeds the bounded size")
        return LocalScheduleConfig.from_external(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("scheduled local config is invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ensure_local_schedule_config(agent_root: str | Path) -> Path:
    """Create the local default only when absent; never rewrite user bytes."""
    root = Path(agent_root)
    destination = root / DEFAULT_LOCAL_CONFIG
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        load_local_schedule_config(destination)
        return destination
    template = root / DEFAULT_LOCAL_TEMPLATE
    seed_local_schedule_config(template, destination)
    return destination


def validate_local_schedule_config_for_upgrade(agent_root: str | Path) -> None:
    """Validate an existing override or the source default before any mutation."""
    root = Path(agent_root)
    destination = root / DEFAULT_LOCAL_CONFIG
    ancestor = destination.parent
    while True:
        try:
            parent_fd = _open_directory_nofollow(ancestor)
            os.close(parent_fd)
            break
        except FileNotFoundError:
            if ancestor == root:
                raise
            ancestor = ancestor.parent
    try:
        destination.lstat()
    except FileNotFoundError:
        template = root / DEFAULT_LOCAL_TEMPLATE
        if template.exists():
            load_local_schedule_config(template)
        return
    load_local_schedule_config(destination)


def seed_local_schedule_config(template: str | Path, destination: str | Path) -> Path:
    """Atomically seed validated default bytes without following/replacing a path."""
    source = Path(template)
    target = Path(destination)
    source_fd = _open_file_nofollow(source)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ValueError("scheduled local config template is not regular")
        raw = _read_all(source_fd, MAX_LOCAL_CONFIG_BYTES + 1)
    finally:
        os.close(source_fd)
    if len(raw) > MAX_LOCAL_CONFIG_BYTES:
        raise ValueError("scheduled local config template exceeds the bounded size")
    try:
        LocalScheduleConfig.from_external(json.loads(raw.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("scheduled local config template is invalid") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = _open_directory_nofollow(target.parent)
    temporary = f".scheduled-local-{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    identity: tuple[int, int] | None = None
    try:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        os.fchmod(fd, 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.link(
                temporary, target.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            load_local_schedule_config(target)
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            current = os.stat(
                temporary, dir_fd=parent_fd, follow_symlinks=False,
            )
            if identity is not None and (current.st_dev, current.st_ino) == identity:
                os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)
    return target


def _read_all(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("scheduled local config write made no progress")
        view = view[written:]


def _open_directory_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    descriptor = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            except OSError as original:
                # macOS exposes root-owned compatibility aliases such as
                # /var -> /private/var. Permit only a root-owned alias whose
                # resolved directory is also root-owned and non-writable by
                # group/other; user-controlled symlinks still fail closed.
                try:
                    link_info = os.stat(
                        component, dir_fd=descriptor, follow_symlinks=False,
                    )
                    root_info = os.stat(absolute.anchor)
                    parent_info = os.fstat(descriptor)
                    if (
                        component not in {"var", "tmp"}
                        or (parent_info.st_dev, parent_info.st_ino)
                        != (root_info.st_dev, root_info.st_ino)
                        or not stat.S_ISLNK(link_info.st_mode)
                        or link_info.st_uid != 0
                    ):
                        raise original
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                        dir_fd=descriptor,
                    )
                    target_info = os.fstat(next_fd)
                    if target_info.st_uid != 0 or target_info.st_mode & 0o022:
                        os.close(next_fd)
                        raise original
                except OSError:
                    raise original
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_file_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parent_fd = _open_directory_nofollow(absolute.parent)
    try:
        return os.open(
            absolute.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
