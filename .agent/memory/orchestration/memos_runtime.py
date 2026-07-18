"""Private, per-project runtime layout for the pinned MemOS local plugin.

Plugin code and behavioral data deliberately have different roots.  The code
root is versioned and may be replaced by an upgrade; a project's HOME and
MEMOS_HOME are never children of that code directory.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


MEMOS_PLUGIN_VERSION = "2.0.10"
_PROJECT_ID = re.compile(r"[0-9a-f]{16}\Z")


@dataclass(frozen=True, slots=True)
class MemosRuntimePaths:
    plugin_dir: Path
    project_root: Path
    memos_home: Path
    home: Path
    config_file: Path


def validate_project_id(project_id: str) -> str:
    """Return an exact canonical ID or reject aliases and path-like values."""
    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("MemOS project ID must be exactly 16 lowercase hexadecimal characters")
    return project_id


def runtime_paths(
    code_root: str | Path,
    data_root: str | Path,
    project_id: str,
) -> MemosRuntimePaths:
    """Derive the immutable code path and isolated mutable project paths."""
    project_id = validate_project_id(project_id)
    code_root = Path(code_root).expanduser().resolve(strict=False)
    data_root = Path(data_root).expanduser().resolve(strict=False)
    plugin_dir = code_root / "memos-local-plugin" / MEMOS_PLUGIN_VERSION
    # Keep the documented MemOS layout directly at
    # ``<data_root>/<project_id>``.  Synthetic process HOME directories are
    # siblings, not children, so upstream code cannot accidentally interpret
    # a HOME cache as MemOS project data.
    project_root = data_root / project_id
    # MemOS 2.0.10's built CommonJS bridge derives feedback ownership only
    # from this supported Hermes profile shape. Keeping the journal at
    # project_root while nesting MemOS data here preserves physical isolation
    # and stamps feedback with the project profile instead of "default".
    memos_home = project_root / "profiles" / project_id / "memos-plugin"
    home = data_root / "homes" / project_id
    return MemosRuntimePaths(
        plugin_dir=plugin_dir,
        project_root=project_root,
        memos_home=memos_home,
        home=home,
        config_file=memos_home / "config.yaml",
    )


def build_memos_config(project_id: str) -> dict:
    """Return the minimal offline/privacy profile accepted by MemOS 2.0.10.

    JSON is intentionally emitted into ``config.yaml``: JSON is a YAML subset,
    avoids adding a YAML dependency, and gives byte-for-byte deterministic
    output.  Credential-shaped optional fields are omitted rather than stored
    as tempting empty slots.
    """
    validate_project_id(project_id)
    return {
        "version": 1,
        "viewer": {
            "bindHost": "127.0.0.1",
            "openOnFirstTurn": False,
        },
        "bridge": {"mode": "stdio"},
        "embedding": {
            "provider": "local",
            "model": "Xenova/all-MiniLM-L6-v2",
            "cache": {"enabled": True, "maxItems": 20_000},
        },
        "llm": {
            "provider": "local_only",
            "fallbackToHost": False,
            "maxRetries": 0,
        },
        "algorithm": {"lightweightMemory": {"enabled": True}},
        "hub": {"enabled": False, "role": "client"},
        "telemetry": {"enabled": False},
        "logging": {
            "level": "info",
            "detailedView": False,
            # stdout is reserved for JSON-RPC framing. MemOS otherwise emits
            # info logs on stdout and corrupts the bridge protocol.
            "console": {"enabled": False, "pretty": False, "channels": []},
            "file": {
                "enabled": True,
                "format": "json",
                "retentionDays": 30,
            },
            "llmLog": {
                "enabled": False,
                "redactPrompts": True,
                "redactCompletions": True,
            },
        },
    }


def write_config_atomic(path: str | Path, config: Mapping) -> Path:
    """Atomically replace ``path`` with deterministic owner-only content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _make_owner_only_directory(path.parent)
    payload = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def prepare_project_runtime(
    code_root: str | Path,
    data_root: str | Path,
    project_id: str,
    *,
    preserve_existing_config: bool = True,
) -> MemosRuntimePaths:
    """Provision only mutable project state; never mutate the plugin tree."""
    paths = runtime_paths(code_root, data_root, project_id)
    for directory in (
        paths.project_root,
        paths.memos_home,
        paths.home,
        paths.memos_home / "data",
        paths.memos_home / "skills",
        paths.memos_home / "logs",
        paths.memos_home / "daemon",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _make_owner_only_directory(directory)
    if not preserve_existing_config or not paths.config_file.exists():
        write_config_atomic(paths.config_file, build_memos_config(project_id))
    else:
        expected = build_memos_config(project_id)
        try:
            existing = json.loads(paths.config_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("existing MemOS config is invalid; refusing to start") from exc
        if existing != expected:
            raise RuntimeError(
                "existing MemOS config differs from the required privacy profile; "
                "refusing to start"
            )
        os.chmod(paths.config_file, 0o600)
    return paths


def runtime_environment(
    paths: MemosRuntimePaths,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an environment without inheriting the operator's HOME."""
    environment = dict(base or {})
    environment.update(
        {
            "HOME": str(paths.home),
            "MEMOS_HOME": str(paths.memos_home),
            "MEMOS_CONFIG_FILE": str(paths.config_file),
            "MEMOS_TELEMETRY_ENABLED": "0",
        }
    )
    return environment


def bridge_command(paths: MemosRuntimePaths, node_command: str = "node") -> tuple[str, ...]:
    """Return the only supported bridge launch shape: stdio and headless.

    MemOS 2.0.10 controls viewer startup with ``--no-viewer`` rather than a
    schema-backed boolean.  Keeping the flag here makes the privacy setting
    executable instead of relying only on documentation/config metadata. The
    packaged CJS bridge is intentional: unlike 2.0.10's MJS build, it derives
    the project profile from MEMOS_HOME so feedback ownership remains scoped.
    """
    return (
        node_command,
        str(
            paths.plugin_dir
            / "node_modules"
            / "@memtensor"
            / "memos-local-plugin"
            / "dist"
            / "bridge.cjs"
        ),
        "--agent=hermes",
        "--no-viewer",
        f"--home={paths.memos_home}",
    )


def _make_owner_only_directory(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Permission enforcement is mandatory for a directory we own.
        raise


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
