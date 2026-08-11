"""Governed Memory + Code Evidence installation profiles."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import scheduled_runtime
from .local_schedule_config import ensure_local_schedule_config


STANDARD = "standard"
MINIMAL = "minimal"
VALID_PROFILES = frozenset({STANDARD, MINIMAL})
ARCHITECTURE = "governed-memory-code-evidence"
_CONFIG = {
    "schema": "agentic.memory.config.v2",
    "architecture": ARCHITECTURE,
    "total_token_budget": 7800,
    "lane_reserves": {"governance": 4800, "evidence": 3000},
    "project_aliases": {},
}


def validate_profile(profile: str) -> str:
    if profile not in VALID_PROFILES:
        raise ValueError(f"unknown installation profile {profile!r}; choose one of {sorted(VALID_PROFILES)}")
    return profile


def minimal_omitted_paths() -> frozenset[str]:
    return frozenset()


def profile_record(profile: str, scheduled_python: str | Path | None = None, *, runtime: scheduled_runtime.ScheduledRuntime | None = None, forbidden_roots: tuple[str | Path, ...] = ()) -> dict[str, object]:
    validate_profile(profile)
    if runtime is not None and scheduled_python is not None:
        raise ValueError("scheduled Python runtime must be selected exactly once")
    selected = runtime or scheduled_runtime.select_runtime(scheduled_python, forbidden_roots=forbidden_roots)
    return {
        "profile": profile,
        "architecture": ARCHITECTURE,
        "providers": ["governance", "crg-evidence"],
        "scheduled_runtime": scheduled_runtime.validate_record_data(selected.record()).record(),
    }


def validate_blocked_profile_state(orchestration: dict[object, object]) -> None:
    _validate_record(orchestration)


def ensure_profile_compatible(profile: str, install_state: object, *, forbidden_roots: tuple[str | Path, ...] = ()) -> None:
    validate_profile(profile)
    orchestration = install_state.get("orchestration") if isinstance(install_state, dict) else None
    if not isinstance(orchestration, dict):
        return
    prior = orchestration.get("profile")
    if isinstance(prior, str) and prior != profile:
        raise ValueError("cannot change installation profile in place; use a fresh project installation")
    _validate_record(orchestration)
    scheduled_runtime.runtime_from_record(orchestration.get("scheduled_runtime"), forbidden_roots=forbidden_roots)


def validate_blocked_configuration(agent_root: Path) -> None:
    path = agent_root / "memory/orchestration/config.json"
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("orchestration config must be a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value != _CONFIG:
        raise ValueError("orchestration config must use Governed Memory + Code Evidence v2")


def validate_existing_install(profile: str, install_state: object, agent_root: Path) -> None:
    ensure_profile_compatible(profile, install_state, forbidden_roots=(agent_root.parent,))
    validate_blocked_configuration(agent_root)


def resolve_upgrade_profile(install_state: object, agent_root: Path) -> tuple[str, bool]:
    if not isinstance(install_state, dict):
        raise ValueError("install.json is missing; run the installer before upgrade")
    orchestration = install_state.get("orchestration")
    if orchestration is None:
        retired_paths = (
            agent_root / "runtime/memos",
            agent_root / "runtime/providers/memos-local-plugin",
            agent_root / "memory/orchestration/memos_factory.py",
        )
        if any(path.exists() or path.is_symlink() for path in retired_paths):
            raise ValueError("run `./install.sh retire-memos --yes` before upgrading this legacy installation")
        return STANDARD, True
    if not isinstance(orchestration, dict):
        raise ValueError("installation profile is malformed")
    if orchestration.get("architecture") is None:
        retired_paths = (
            agent_root / "runtime/memos",
            agent_root / "runtime/providers/memos-local-plugin",
            agent_root / "memory/orchestration/memos_factory.py",
        )
        if any(path.exists() or path.is_symlink() for path in retired_paths):
            raise ValueError("run `./install.sh retire-memos --yes` before upgrading this legacy installation")
        profile = validate_profile(str(orchestration.get("profile", STANDARD)))
        return profile, True
    _validate_record(orchestration)
    profile = validate_profile(str(orchestration.get("profile")))
    scheduled_runtime.runtime_from_record(orchestration.get("scheduled_runtime"), forbidden_roots=(agent_root.parent,))
    validate_blocked_configuration(agent_root)
    return profile, False


def includes_infrastructure(relative: Path, profile: str) -> bool:
    validate_profile(profile)
    return True


def copy_brain(source: Path, destination: Path, *, profile: str) -> None:
    validate_profile(profile)
    if destination.exists():
        raise FileExistsError(f"brain destination already exists: {destination}")
    shutil.copytree(source, destination)
    ensure_local_schedule_config(destination)
    config = destination / "memory/orchestration/config.json"
    config.write_text(json.dumps(_CONFIG, indent=2) + "\n", encoding="utf-8")


def infrastructure_bytes(source: Path, profile: str) -> bytes:
    validate_profile(profile)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("infrastructure inventory must be an object")
    return (json.dumps(value, indent=2) + "\n").encode()


def _validate_record(record: dict[object, object]) -> None:
    if record.get("architecture") != ARCHITECTURE:
        raise ValueError("installation has not completed MemOS retirement")
    if record.get("providers") != ["governance", "crg-evidence"]:
        raise ValueError("installation provider set must contain governance and CRG evidence only")
    forbidden = {str(key).casefold() for key in record} & {"memos_capability", "memos_mode", "evolution_enabled", "r7_skill_promoted"}
    if forbidden:
        raise ValueError("retired provider fields remain in installation state")
