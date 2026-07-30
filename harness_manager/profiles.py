"""Installation profiles for the portable agentic-stack brain.

Profiles are an installer boundary, not a runtime activation mechanism.  In
particular, the Phase 8 quality result is a hard stop: the standard profile
can carry the reviewed MemOS implementation, but it records it as unavailable
for assist/evolution/promoted-skill use.
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from . import scheduled_runtime
from .local_schedule_config import ensure_local_schedule_config


STANDARD = "standard"
MINIMAL = "minimal"
VALID_PROFILES = frozenset({STANDARD, MINIMAL})
PHASE8_QUALITY_GATE = "blocked"

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
    config_path = agent_root / "memory" / "orchestration" / "config.json"
    if not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    ensure_local_schedule_config(destination)
    if profile == MINIMAL:
        copy_infrastructure(source / "infrastructure.json", destination / "infrastructure.json", profile)


def _ignore_for(source: Path, profile: str) -> Callable[[str, list[str]], set[str]]:
    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).relative_to(source)
        omitted = {name for name in names if name == "__pycache__"}
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
    required = (
        "memory/orchestration/memos_factory.py",
        "memory/orchestration/providers/memos_local.py",
    )
    if any(not (agent_root / relative).is_file() for relative in required):
        raise ValueError(
            "unprofiled installation cannot be safely migrated to standard; "
            "reinstall with --profile minimal or --profile standard"
        )
    config_path = agent_root / "memory/orchestration/config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError(
            "unprofiled installation has no valid orchestration config; "
            "reinstall with an explicit profile"
        ) from None
    if not isinstance(config, dict) or config.get("mode") != "off":
        raise ValueError(
            "unprofiled installation is not off/governance-only; "
            "set mode to off and reinstall with an explicit profile"
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
