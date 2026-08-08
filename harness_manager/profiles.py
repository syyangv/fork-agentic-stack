"""Installation profiles for the portable agentic-stack brain.

Profiles are an installer boundary, not a runtime activation mechanism.  In
particular, the Phase 8 quality result is a hard stop: the standard profile
can carry the reviewed MemOS implementation, but it records it as unavailable
for assist/evolution/promoted-skill use.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from . import scheduled_runtime
from .local_schedule_config import ensure_local_schedule_config


STANDARD = "standard"
MINIMAL = "minimal"
VALID_PROFILES = frozenset({STANDARD, MINIMAL})
PHASE8_QUALITY_GATE = "blocked"
_PHASE2_STACK_VERSION = "0.18.0"
_PHASE2_FEATURES = (
    "latest_state_recall",
    "serialized_candidate_lifecycle",
    "staging_only_scheduled_review",
    "structured_dream_health",
    "crg_registration_health",
    "memory_contracts_v1",
    "memory_redaction",
    "stable_project_identity",
    "deterministic_memory_routing",
    "bounded_lane_budgets",
    "strict_orchestration_config",
    "governance_provider",
    "governance_orchestrator_cli",
    "legacy_recall_comparison",
)
_PHASE2_CONFIG = {
    "schema": "agentic.memory.config.v1",
    "mode": "off",
    "total_token_budget": 12000,
    "lane_reserves": {
        "governance": 4800,
        "behavioral": 4200,
        "evidence": 3000,
    },
    "project_aliases": {},
}

# These are the optional behavioral-provider implementation and its only
# entrypoint. The governance orchestrator remains installed in minimal mode.
_MINIMAL_OMIT = frozenset({
    "memory/orchestration/assist_gate.py",
    "memory/orchestration/evolution_eval.py",
    "memory/orchestration/host_evolution.py",
    "memory/orchestration/memos_backup.py",
    "memory/orchestration/memos_bridge.py",
    "memory/orchestration/memos_factory.py",
    "memory/orchestration/memos_journal.py",
    "memory/orchestration/memos_runtime.py",
    "memory/orchestration/promotion.py",
    "memory/orchestration/providers/memos_local.py",
})

# Volatile runtime state. copy_brain() hands shutil.copytree this repo's own
# .agent/, so whatever the working tree holds when a release is cut ships
# verbatim to every user. That is not hypothetical: master currently carries a
# WORKSPACE.md describing this project's Phase 9 gating and an episodic log
# naming internal pilot runs, and a tree mid-task accretes far more. Users were
# receiving one repo's task state installed as if it were their own.
#
# A brain is only "fresh", as the copy_brain docstring already claims, if these
# are reset at copy time. Vetted knowledge is deliberately NOT volatile:
# memory/candidates/graduated/ and memory/semantic/lessons.jsonl are curated
# seed lessons and must survive the reset.
#
# NOTE: this template is duplicated in .agent/memory/archive.py, which reseeds
# the same file after the dream cycle archives it. Unify once both land.
_FRESH_WORKSPACE = """# Workspace (live task state)

> Replace this template on your first real task. The dream cycle auto-archives
> this file after 2 days of inactivity — don't keep long-lived notes here.

## Current task
-

## Open files
-

## Active hypotheses
-

## Checkpoints
-

## Next step
-
"""

_FRESH_REVIEW_QUEUE = """# Review Queue

_No pending candidates._
"""

_VOLATILE_BRAIN_FILES: dict[str, str] = {
    "memory/working/WORKSPACE.md": _FRESH_WORKSPACE,
    "memory/working/REVIEW_QUEUE.md": _FRESH_REVIEW_QUEUE,
    "memory/episodic/AGENT_LEARNINGS.jsonl": "",
}

# Transient by construction: dream-cycle snapshots of past workspaces, and
# candidates that were rejected rather than graduated.
_TRANSIENT_BRAIN_DIRS = frozenset({
    "memory/episodic/snapshots",
    "memory/candidates/rejected",
})

# Per-project runtime coordination and health state. `.agent/.gitignore`
# already declares exactly this set as "not content", but copytree does not
# read .gitignore, so a brain copied from a working clone shipped one host's
# dream-cycle run ids, lifecycle locks and half-finished upgrade transactions.
# A stale .upgrade-transaction.json is the dangerous one: it lands in a fresh
# install describing an upgrade that never happened here.
#
# Kept as an explicit list rather than parsed from .gitignore, because the
# installer should not depend on a git file at runtime. The two are pinned
# together by test_runtime_omissions_match_agent_gitignore, so adding a path
# to .agent/.gitignore without adding it here fails the suite.
_RUNTIME_STATE_PATHS = frozenset({
    ".upgrade-transaction.json",
    "memory/candidates/.lifecycle.lock",
    "memory/dream-state.json",
    "memory/evidence/revalidation.sqlite3",
    "memory/orchestration/scheduled-local.json",
    "runtime",
})

# Staged candidates awaiting review live as loose JSON directly under
# candidates/; only the graduated/ subdirectory is curated seed knowledge.
_STAGED_CANDIDATE_DIR = "memory/candidates"


def validate_profile(profile: str) -> str:
    if profile not in VALID_PROFILES:
        raise ValueError(
            f"unknown installation profile {profile!r}; "
            f"choose one of {sorted(VALID_PROFILES)}"
        )
    return profile


def validate_blocked_profile_state(orchestration: dict[object, object]) -> None:
    """Public read-only Phase 8 invariant check for installer diagnostics."""
    _validate_blocked_state(orchestration)


def minimal_omitted_paths() -> frozenset[str]:
    """Return the provider files a minimal brain must not contain."""
    return _MINIMAL_OMIT


def runtime_state_paths() -> frozenset[str]:
    """Return the per-project runtime state a copied brain must not carry."""
    return _RUNTIME_STATE_PATHS


def profile_record(
    profile: str,
    scheduled_python: str | Path | None = None,
    *,
    runtime: scheduled_runtime.ScheduledRuntime | None = None,
    forbidden_roots: tuple[str | Path, ...] = (),
) -> dict[str, object]:
    """Return the persisted non-authoritative state for an install profile."""
    validate_profile(profile)
    if runtime is not None and scheduled_python is not None:
        raise ValueError("scheduled Python runtime must be selected exactly once")
    selected = runtime or scheduled_runtime.select_runtime(
        scheduled_python, forbidden_roots=forbidden_roots,
    )
    runtime_record = scheduled_runtime.validate_record_data(selected.record()).record()
    if profile == MINIMAL:
        return {
            "profile": MINIMAL,
            "phase8_quality_gate": PHASE8_QUALITY_GATE,
            "governance_only": True,
            "scheduled_runtime": runtime_record,
        }
    return {
        "profile": STANDARD,
        "phase8_quality_gate": PHASE8_QUALITY_GATE,
        "memos_capability": "available",
        "memos_mode": "off",
        "evolution_enabled": False,
        "r7_skill_promoted": False,
        "scheduled_runtime": runtime_record,
    }


def ensure_profile_compatible(
    profile: str, install_state: object, *,
    forbidden_roots: tuple[str | Path, ...] = (),
) -> None:
    """Fail before mutation when a requested profile would misdescribe a brain."""
    validate_profile(profile)
    if not isinstance(install_state, dict):
        return
    orchestration = install_state.get("orchestration")
    if not isinstance(orchestration, dict):
        return
    prior = orchestration.get("profile")
    if isinstance(prior, str) and prior != profile:
        raise ValueError(
            "cannot change installation profile in place; "
            "use a fresh project installation"
        )
    _validate_blocked_state(orchestration)
    scheduled_runtime.runtime_from_record(
        orchestration.get("scheduled_runtime"), forbidden_roots=forbidden_roots,
    )


def validate_blocked_configuration(agent_root: Path) -> None:
    """Reject active behavior before a Phase 8-blocked maintenance action."""
    relative = Path("memory/orchestration/config.json")
    if _agent_entry_is_absent(agent_root, relative):
        return
    try:
        config = _load_agent_json_no_follow(agent_root, relative)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError(
            "Phase 8 quality gate is blocked: orchestration config is invalid; "
            "restore a valid off configuration before installing or upgrading"
        ) from None
    if not isinstance(config, dict):
        raise ValueError(
            "Phase 8 quality gate is blocked: orchestration config must be an object"
        )
    mode = config.get("mode", "off")
    if mode in {"shadow", "assist"}:
        raise ValueError(
            f"Phase 8 quality gate is blocked: orchestration mode {mode!r} is active; "
            "set mode to 'off' before installing or upgrading"
        )
    evolution = config.get("evolution")
    if config.get("evolution_enabled") is True or (
        isinstance(evolution, dict) and evolution.get("enabled") is True
    ):
        raise ValueError(
            "Phase 8 quality gate is blocked: evolution is enabled; "
            "disable evolution before installing or upgrading"
        )


def validate_existing_install(profile: str, install_state: object, agent_root: Path) -> None:
    """Validate an existing brain before install can attach new adapter state."""
    validate_profile(profile)
    ensure_profile_compatible(
        profile, install_state, forbidden_roots=(agent_root.parent,),
    )
    validate_blocked_configuration(agent_root)
    orchestration = install_state.get("orchestration") if isinstance(install_state, dict) else None
    recorded = orchestration.get("profile") if isinstance(orchestration, dict) else None
    if isinstance(recorded, str):
        return
    if profile == MINIMAL:
        raise ValueError(
            "minimal profile requires a fresh project installation; "
            "existing capability is never removed in place"
        )
    _validate_legacy_standard(agent_root)


def resolve_upgrade_profile(install_state: object, agent_root: Path) -> tuple[str, bool]:
    """Return the recorded profile, or a verified standard legacy migration.

    The bool is true only when the caller must persist the explicit migration
    record after every planned copy succeeds.
    """
    if not isinstance(install_state, dict):
        raise ValueError("install.json is missing; run the installer before upgrade")
    orchestration = install_state.get("orchestration")
    if orchestration is None:
        _validate_legacy_standard(agent_root)
        return STANDARD, True
    if not isinstance(orchestration, dict):
        raise ValueError("installation profile is malformed; reinstall with an explicit profile")
    profile = orchestration.get("profile")
    if not isinstance(profile, str):
        raise ValueError("installation profile is missing; reinstall with an explicit profile")
    validate_profile(profile)
    if profile == MINIMAL:
        _validate_minimal_layout(agent_root)
    _validate_blocked_state(orchestration)
    scheduled_runtime.runtime_from_record(
        orchestration.get("scheduled_runtime"),
        forbidden_roots=(agent_root.parent,),
    )
    validate_blocked_configuration(agent_root)
    return profile, False


def includes_infrastructure(relative: Path, profile: str) -> bool:
    """Whether a source infrastructure path belongs to the profile."""
    validate_profile(profile)
    return profile != MINIMAL or relative.as_posix() not in _MINIMAL_OMIT


def copy_brain(source: Path, destination: Path, *, profile: str) -> None:
    """Copy a fresh portable brain, excluding optional capability for minimal."""
    validate_profile(profile)
    if destination.exists():
        raise FileExistsError(f"brain destination already exists: {destination}")

    shutil.copytree(source, destination, ignore=_ignore_for(source, profile))
    _seed_fresh_state(destination)
    ensure_local_schedule_config(destination)
    if profile == MINIMAL:
        copy_infrastructure(source / "infrastructure.json", destination / "infrastructure.json", profile)


def _is_backup_artifact(name: str) -> bool:
    """Editor and script backups are one tree's history, not shipped content.

    The repo currently tracks `reviewed-drift.json.bak-20260603`, so these
    reach users through a release tarball rather than only a dirty clone.
    """
    return name.endswith(".bak") or ".bak-" in name or name.endswith("~")


def _seed_fresh_state(destination: Path) -> None:
    """Reset volatile runtime state so an installed brain starts empty.

    Seeded unconditionally rather than only where the source happened to
    have a file: every adapter's instructions tell agents to update
    WORKSPACE.md, so the path must exist even if the source lacked it.
    """
    for relative, contents in _VOLATILE_BRAIN_FILES.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


def _ignore_for(source: Path, profile: str) -> Callable[[str, list[str]], set[str]]:
    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).relative_to(source)
        omitted = {name for name in names if name == "__pycache__"}
        omitted.update(
            name for name in names
            if (relative / name).as_posix() in _TRANSIENT_BRAIN_DIRS
            or (relative / name).as_posix() in _RUNTIME_STATE_PATHS
            or _is_backup_artifact(name)
        )
        if relative.as_posix() == _STAGED_CANDIDATE_DIR:
            # graduated/ is a directory and survives; loose JSON is unvetted.
            omitted.update(name for name in names if name.endswith(".json"))
        if profile == MINIMAL:
            omitted.update(
                name for name in names
                if not includes_infrastructure(relative / name, profile)
            )
        return omitted

    return ignore


def copy_infrastructure(source: Path, destination: Path, profile: str) -> None:
    """Copy infrastructure inventory, filtering unavailable capability names."""
    destination.write_bytes(infrastructure_bytes(source, profile))


def infrastructure_bytes(source: Path, profile: str) -> bytes:
    """Render the exact profile-specific infrastructure representation."""
    validate_profile(profile)
    if profile != MINIMAL:
        return source.read_bytes()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("minimal profile infrastructure inventory is invalid") from None
    if not isinstance(data, dict):
        raise ValueError("minimal profile infrastructure inventory must be an object")
    features = data.get("features")
    if isinstance(features, list):
        data["features"] = [
            feature for feature in features
            if not (isinstance(feature, str) and feature.startswith("memos_"))
        ]
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_minimal_layout(agent_root: Path) -> None:
    restored = [
        relative for relative in _MINIMAL_OMIT
        if (agent_root / relative).exists()
    ]
    if restored:
        raise ValueError(
            "minimal installation contains behavioral capability files; "
            "reinstall a fresh minimal profile"
        )


def _validate_legacy_standard(agent_root: Path) -> None:
    capability_present = [
        _is_regular_agent_file(agent_root, Path(relative))
        for relative in _MINIMAL_OMIT
    ]
    if not all(capability_present) and (
        any(capability_present) or not _is_deployed_phase2_governance_brain(agent_root)
    ):
        raise ValueError(
            "unprofiled installation cannot be safely migrated to standard; "
            "reinstall with --profile minimal or --profile standard"
        )
    validate_blocked_configuration(agent_root)
    try:
        config = _load_agent_json_no_follow(
            agent_root, Path("memory/orchestration/config.json"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError(
            "unprofiled installation has no valid orchestration config; "
            "reinstall with an explicit profile"
        ) from None
    if not isinstance(config, dict) or config.get("mode") != "off":
        raise ValueError(
            "unprofiled installation is not off/governance-only; "
            "set mode to off and reinstall with an explicit profile"
        )


def _is_deployed_phase2_governance_brain(agent_root: Path) -> bool:
    """Recognize the exact pre-MemOS deployment eligible for standard upgrade."""
    if any(
        not _agent_entry_is_absent(agent_root, Path(relative))
        for relative in _MINIMAL_OMIT
    ):
        return False
    try:
        inventory = _load_agent_json_no_follow(
            agent_root, Path("infrastructure.json"),
        )
        config = _load_agent_json_no_follow(
            agent_root, Path("memory/orchestration/config.json"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(inventory, dict)
        and set(inventory) == {
            "schema_version", "stack_version", "orchestration_phase", "features",
        }
        and type(inventory["schema_version"]) is int
        and inventory["schema_version"] == 1
        and inventory["stack_version"] == _PHASE2_STACK_VERSION
        and type(inventory["orchestration_phase"]) is int
        and inventory["orchestration_phase"] == 2
        and inventory["features"] == list(_PHASE2_FEATURES)
        and _matches_phase2_config(config)
    )


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_MAX_MIGRATION_JSON_BYTES = 64 * 1024


def _is_reparse_point(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(info, "st_file_attributes", 0) & marker)


def _descriptor_relative_supported() -> bool:
    return bool(
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
    )


def _portable_agent_path(agent_root: Path, relative: Path) -> Path:
    """Resolve a migration path without following existing symlinks."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("migration path is invalid")
    root = _safe_lexical_absolute(agent_root)
    current = Path(root.anchor)
    for component in root.parts[1:]:
        current /= component
        info = current.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or _is_reparse_point(info)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("migration path must not traverse symbolic links")
    for component in relative.parts[:-1]:
        current /= component
        info = current.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or _is_reparse_point(info)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("migration path must not traverse symbolic links")
    return current / relative.parts[-1]


def _open_agent_parent(agent_root: Path, relative: Path) -> tuple[int, str]:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("migration path is invalid")
    root = _safe_lexical_absolute(agent_root)
    descriptor = os.open(root.anchor, _DIR_FLAGS)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        for component in relative.parts[:-1]:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor, relative.parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _safe_lexical_absolute(path: str | Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if sys.platform == "darwin" and (
        absolute == Path("/var") or Path("/var") in absolute.parents
    ):
        absolute = Path("/private/var").joinpath(*absolute.parts[2:])
    return absolute


def _is_regular_agent_file(agent_root: Path, relative: Path) -> bool:
    if not _descriptor_relative_supported():
        try:
            info = _portable_agent_path(agent_root, relative).lstat()
            return bool(
                stat.S_ISREG(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and not _is_reparse_point(info)
            )
        except (OSError, ValueError):
            return False
    descriptor = file_fd = -1
    try:
        descriptor, name = _open_agent_parent(agent_root, relative)
        file_fd = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
        return stat.S_ISREG(os.fstat(file_fd).st_mode)
    except OSError:
        return False
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if descriptor >= 0:
            os.close(descriptor)


def _agent_entry_is_absent(agent_root: Path, relative: Path) -> bool:
    if not _descriptor_relative_supported():
        try:
            _portable_agent_path(agent_root, relative).lstat()
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            return False
        return False
    descriptor = -1
    try:
        descriptor, name = _open_agent_parent(agent_root, relative)
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return False


def _load_agent_json_no_follow(agent_root: Path, relative: Path) -> object:
    if not _descriptor_relative_supported():
        path = _portable_agent_path(agent_root, relative)
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or _is_reparse_point(info)
            or not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("migration JSON path is not a regular file")
        encoded = path.read_bytes()
        if len(encoded) > _MAX_MIGRATION_JSON_BYTES:
            raise ValueError("migration JSON exceeds size bound")
        return json.loads(encoded.decode("utf-8"))
    descriptor = file_fd = -1
    try:
        descriptor, name = _open_agent_parent(agent_root, relative)
        file_fd = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("migration JSON path is not a regular file")
        chunks: list[bytes] = []
        remaining = _MAX_MIGRATION_JSON_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > _MAX_MIGRATION_JSON_BYTES:
            raise ValueError("migration JSON exceeds size bound")
        return json.loads(encoded.decode("utf-8"))
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if descriptor >= 0:
            os.close(descriptor)


def _matches_phase2_config(config: object) -> bool:
    if not isinstance(config, dict) or set(config) != set(_PHASE2_CONFIG):
        return False
    reserves = config.get("lane_reserves")
    return bool(
        config.get("schema") == _PHASE2_CONFIG["schema"]
        and config.get("mode") == "off"
        and type(config.get("total_token_budget")) is int
        and config.get("total_token_budget") == 12000
        and isinstance(reserves, dict)
        and set(reserves) == {"governance", "behavioral", "evidence"}
        and all(type(reserves.get(lane)) is int for lane in reserves)
        and reserves == _PHASE2_CONFIG["lane_reserves"]
        and type(config.get("project_aliases")) is dict
        and config.get("project_aliases") == {}
    )


def _validate_blocked_state(orchestration: dict[object, object]) -> None:
    if orchestration.get("phase8_quality_gate") != PHASE8_QUALITY_GATE:
        raise ValueError(
            "Phase 8 quality gate must be recorded as 'blocked' before installing or upgrading"
        )
    mode = orchestration.get("memos_mode")
    if mode in {"shadow", "assist"}:
        raise ValueError(
            f"Phase 8 quality gate is blocked: recorded behavioral mode {mode!r} is active; "
            "set it to 'off' before installing or upgrading"
        )
    if orchestration.get("evolution_enabled") is True:
        raise ValueError(
            "Phase 8 quality gate is blocked: evolution is enabled; "
            "disable evolution before installing or upgrading"
        )
    if orchestration.get("r7_skill_promoted") is True:
        raise ValueError(
            "Phase 8 quality gate is blocked: the R7 skill is promoted; "
            "unpromote it before installing or upgrading"
        )
