"""Private, per-project runtime layout for the pinned MemOS local plugin.

Plugin code and behavioral data deliberately have different roots.  The code
root is versioned and may be replaced by an upgrade; a project's HOME and
MEMOS_HOME are never children of that code directory.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .memos_journal import _project_lock, stable_project_lock_path


MEMOS_PLUGIN_VERSION = "2.0.10"
MEMOS_PLUGIN_SHASUM = "d75850ce7340d56b8a255831969950b9fbf96995"
MEMOS_PLUGIN_INTEGRITY = (
    "sha512-Rg2NIjGAObTC3zFQ4wOzB+hxR7qHvHWMVI5Nxc+7QEi5wpBUibkniz3SdHOPrbbCkqhatS0DjZ+aUexl/9Q+EA=="
)
MEMOS_PINNED_FILE_SHA256 = {
    "node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs":
        "fc58eb07a35b6fec9f74646f98dca90ac5576d43ed2d87cad211241efc8a8ad7",
    "node_modules/@memtensor/memos-local-plugin/package.json":
        "23455d0245a681f2939236451cf23cb02593c0f0b80413374bd5cfea197f90c2",
    "package-lock.json":
        "4da221c70a06c5a14948af73c31661957bb7a36832ab764ee6a3c884cd0e7c2b",
}
_PLUGIN_MANIFEST = ".agentic-stack-files.json"
_PLUGIN_MARKER = ".agentic-stack-install.json"
_PROJECT_ID = re.compile(r"[0-9a-f]{16}\Z")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_PILOT_SCHEMA = "agentic.memory.evolution-pilot.v2"
_PILOT_KEYS = {
    "schema", "enabled", "project_id", "repo_root", "provider", "model",
    "daily_caps", "min_distinct_episodes", "timeout_seconds",
}
_DAILY_CAP_KEYS = {"policy", "world_model", "skill", "other"}


@dataclass(frozen=True, slots=True)
class EvolutionPilotConfig:
    """Validated, project-bound opt-in for host-assisted evolution."""

    project_id: str
    repo_root: str
    provider: str
    model: str
    daily_caps: Mapping[str, int]
    min_distinct_episodes: int
    timeout_seconds: float
    source: Path


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


def validate_pinned_plugin(plugin_dir: str | Path) -> Path:
    """Require the exact installer-attested immutable MemOS 2.0.10 tree."""
    root = Path(plugin_dir)
    _reject_symlink_components(root)
    package_dir = root / "node_modules/@memtensor/memos-local-plugin"
    bridge = package_dir / "dist/bridge.cjs"
    try:
        marker = json.loads((root / _PLUGIN_MARKER).read_text("utf-8"))
        manifest_bytes = (root / _PLUGIN_MANIFEST).read_bytes()
        manifest = json.loads(manifest_bytes)
        package = json.loads((package_dir / "package.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("pinned MemOS 2.0.10 install is unavailable") from exc
    expected_marker = {
        "artifact_sha1": MEMOS_PLUGIN_SHASUM,
        "integrity": MEMOS_PLUGIN_INTEGRITY,
        "package": "@memtensor/memos-local-plugin",
        "version": MEMOS_PLUGIN_VERSION,
    }
    manifest_digest = marker.pop("files_manifest_sha256", None)
    if (marker != expected_marker
            or not isinstance(manifest_digest, str)
            or not hmac.compare_digest(
                manifest_digest, hashlib.sha256(manifest_bytes).hexdigest()
            )
            or package.get("version") != MEMOS_PLUGIN_VERSION
            or not bridge.is_file()):
        raise RuntimeError("pinned MemOS 2.0.10 install attestation is invalid")
    if manifest != build_plugin_file_manifest(root):
        raise RuntimeError("pinned MemOS 2.0.10 file inventory mismatch")
    if root.is_symlink() or bridge.is_symlink() or root.stat().st_mode & 0o222:
        raise RuntimeError("pinned MemOS 2.0.10 install must be immutable")
    for relative, expected_digest in MEMOS_PINNED_FILE_SHA256.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("pinned MemOS 2.0.10 required file is unsafe")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_digest:
            raise RuntimeError("pinned MemOS 2.0.10 required file digest mismatch")
    resolved_root = root.resolve(strict=True)
    for directory, directories, files in os.walk(root):
        for name in (*directories, *files):
            path = Path(directory) / name
            if path.is_symlink():
                try:
                    target = path.resolve(strict=True)
                except OSError as exc:
                    raise RuntimeError("pinned MemOS tree has a broken symlink") from exc
                if not target.is_relative_to(resolved_root):
                    raise RuntimeError("pinned MemOS tree symlink escapes its root")
            elif path.stat().st_mode & 0o222:
                raise RuntimeError("pinned MemOS 2.0.10 install contains writable code")
    return bridge


def build_plugin_file_manifest(root: str | Path) -> dict[str, dict[str, object]]:
    """Return an exact path/type/size/digest inventory for installed code."""
    root = Path(root)
    entries: dict[str, dict[str, object]] = {}
    for directory, directories, files in os.walk(root):
        directories.sort()
        files.sort()
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative in {_PLUGIN_MANIFEST, _PLUGIN_MARKER}:
                continue
            if path.is_symlink():
                entries[relative] = {"type": "symlink", "target": os.readlink(path)}
                continue
            if not path.is_file():
                raise RuntimeError("pinned MemOS tree contains a non-regular file")
            entries[relative] = {
                "type": "file",
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    return entries


def build_memos_config(
    project_id: str,
    *,
    evolution_pilot: bool = False,
    host_model: str = "gpt",
    min_distinct_episodes: int = 3,
) -> dict:
    """Return the minimal offline/privacy profile accepted by MemOS 2.0.10.

    JSON is intentionally emitted into ``config.yaml``: JSON is a YAML subset,
    avoids adding a YAML dependency, and gives byte-for-byte deterministic
    output.  Credential-shaped optional fields are omitted rather than stored
    as tempting empty slots.
    """
    validate_project_id(project_id)
    if not isinstance(evolution_pilot, bool):
        raise TypeError("evolution_pilot must be boolean")
    if _MODEL_NAME.fullmatch(host_model) is None:
        raise ValueError("host model must be a non-sensitive routing label")
    if type(min_distinct_episodes) is not int or not 3 <= min_distinct_episodes <= 20:
        raise ValueError("min_distinct_episodes must be between 3 and 20")
    config = {
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
            "provider": "host" if evolution_pilot else "local_only",
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
    if evolution_pilot:
        config["llm"]["model"] = host_model
        config["algorithm"] = {
            "lightweightMemory": {"enabled": False},
            "capture": {
                "alphaScoring": False,
                "synthReflections": False,
                "batchMode": "per_step",
            },
            "reward": {"llmScoring": False},
            "l2Induction": {"minEpisodesForInduction": min_distinct_episodes},
            "l3Abstraction": {
                "minPolicies": 2, "minPolicySupport": 3,
                "traceEvidencePerPolicy": 0,
            },
            "skill": {"minSupport": 3, "candidateTrials": 3},
            "feedback": {"useLlm": False},
            "retrieval": {"llmFilterEnabled": False},
        }
    return config


def load_evolution_pilot_config(
    path: str | Path,
    *,
    project_id: str,
    repo_root: str | Path,
) -> EvolutionPilotConfig:
    """Load one owner-only pilot selection and bind it to the current repo.

    The file is intentionally an exact-schema document. Unknown fields are
    rejected, which also prevents credentials from becoming configuration.
    """
    validate_project_id(project_id)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("evolution pilot config path must be absolute")
    _reject_symlink_components(candidate)
    try:
        metadata = candidate.stat()
    except OSError as exc:
        raise RuntimeError("evolution pilot config is unavailable") from exc
    if not candidate.is_file() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("evolution pilot config must be a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise PermissionError("evolution pilot config must be owner-only")
    if metadata.st_size > 16_384:
        raise ValueError("evolution pilot config is too large")
    try:
        payload = json.loads(
            candidate.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("evolution pilot config is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _PILOT_KEYS:
        raise ValueError("evolution pilot config has an unsupported schema shape")
    if payload["schema"] != _PILOT_SCHEMA or payload["enabled"] is not True:
        raise ValueError("evolution pilot config must be explicitly enabled")
    if payload["project_id"] != project_id:
        raise ValueError("evolution pilot config project_id does not match")
    try:
        root_path = Path(repo_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("evolution pilot repo_root is unavailable") from exc
    if not root_path.is_dir():
        raise ValueError("evolution pilot repo_root must be a directory")
    canonical_root = str(root_path)
    if payload["repo_root"] != canonical_root:
        raise ValueError("evolution pilot config repo_root does not match")
    if payload["provider"] != "claude_opus":
        raise ValueError("evolution pilot provider must be claude_opus")
    if not isinstance(payload["model"], str) or _MODEL_NAME.fullmatch(payload["model"]) is None:
        raise ValueError("evolution pilot model is invalid")
    if "opus" not in payload["model"].lower():
        raise ValueError("evolution pilot model must be an Opus routing label")
    caps = payload["daily_caps"]
    if not isinstance(caps, dict) or set(caps) != _DAILY_CAP_KEYS:
        raise ValueError("evolution pilot daily_caps has an unsupported shape")
    if any(type(value) is not int or not 0 <= value <= 10_000 for value in caps.values()):
        raise ValueError("evolution pilot daily caps must be bounded integers")
    episodes = payload["min_distinct_episodes"]
    timeout = payload["timeout_seconds"]
    if type(episodes) is not int or not 3 <= episodes <= 20:
        raise ValueError("min_distinct_episodes must be between 3 and 20")
    if type(timeout) not in (int, float) or not 1 <= timeout <= 300:
        raise ValueError("timeout_seconds must be between 1 and 300")
    return EvolutionPilotConfig(
        project_id=project_id,
        repo_root=canonical_root,
        provider=payload["provider"],
        model=payload["model"],
        daily_caps=MappingProxyType(dict(caps)),
        min_distinct_episodes=episodes,
        timeout_seconds=float(timeout),
        source=candidate,
    )


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
    evolution_pilot: bool = False,
    host_model: str = "gpt",
    min_distinct_episodes: int = 3,
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
    expected = build_memos_config(
        project_id,
        evolution_pilot=evolution_pilot,
        host_model=host_model,
        min_distinct_episodes=min_distinct_episodes,
    )
    # The overwhelmingly common same-profile path is read-only and must not
    # contend with an active delivery worker (assist has a subsecond budget).
    # A real profile switch is serialized below and rechecked under the lock.
    if preserve_existing_config and paths.config_file.exists():
        existing = _read_existing_config(paths.config_file)
        if existing == expected:
            os.chmod(paths.config_file, 0o600)
            return paths
        if not _is_managed_alternate_config(existing, project_id):
            raise RuntimeError(
                "existing MemOS config differs from the required privacy profile; "
                "refusing to start"
            )

    # A profile switch changes how the next event is processed. Serialize it
    # with delivery so a second compliant bridge cannot keep running against
    # configuration that was rewritten underneath it.
    with _project_lock(stable_project_lock_path(paths.project_root)):
        if not preserve_existing_config or not paths.config_file.exists():
            write_config_atomic(paths.config_file, expected)
        else:
            existing = _read_existing_config(paths.config_file)
            if existing != expected and _is_managed_alternate_config(existing, project_id):
                write_config_atomic(paths.config_file, expected)
            elif existing != expected:
                raise RuntimeError(
                    "existing MemOS config differs from the required privacy profile; "
                    "refusing to start"
                )
            os.chmod(paths.config_file, 0o600)
    return paths


def _read_existing_config(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing MemOS config is invalid; refusing to start") from exc


def _is_managed_alternate_config(value: object, project_id: str) -> bool:
    if value == build_memos_config(project_id):
        return True
    if not isinstance(value, dict):
        return False
    llm = value.get("llm")
    model = llm.get("model") if isinstance(llm, dict) else None
    return (
        isinstance(model, str)
        and _MODEL_NAME.fullmatch(model) is not None
        and isinstance(value.get("algorithm"), dict)
        and isinstance(value["algorithm"].get("l2Induction"), dict)
        and type(value["algorithm"]["l2Induction"].get("minEpisodesForInduction")) is int
        and 3 <= value["algorithm"]["l2Induction"]["minEpisodesForInduction"] <= 20
        and value == build_memos_config(
            project_id,
            evolution_pilot=True,
            host_model=model,
            min_distinct_episodes=value["algorithm"]["l2Induction"]["minEpisodesForInduction"],
        )
    )


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise ValueError("evolution pilot config path cannot contain symlinks")
        except OSError as exc:
            raise ValueError("cannot validate evolution pilot config path") from exc


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("evolution pilot config contains duplicate keys")
        result[key] = value
    return result


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
