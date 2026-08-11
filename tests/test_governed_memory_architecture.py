from __future__ import annotations

import json
from pathlib import Path

from harness_manager import profiles
from harness_manager.doctor import validate_orchestration_config_data


def test_profiles_install_governance_and_evidence_only(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / ".agent"
    destination = tmp_path / ".agent"
    profiles.copy_brain(source, destination, profile=profiles.STANDARD)
    config = json.loads((destination / "memory/orchestration/config.json").read_text())
    assert config["architecture"] == "governed-memory-code-evidence"
    assert set(config["lane_reserves"]) == {"governance", "evidence"}
    assert not (destination / "memory/orchestration/memos_factory.py").exists()


def test_profile_record_has_no_activation_fields() -> None:
    record = profiles.profile_record(profiles.STANDARD)
    assert record["providers"] == ["governance", "crg-evidence"]
    assert not ({"memos_mode", "evolution_enabled", "r7_skill_promoted"} & set(record))


def test_doctor_accepts_governance_evidence_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "schema": "agentic.memory.config.v2",
        "architecture": "governed-memory-code-evidence",
        "total_token_budget": 7800,
        "lane_reserves": {"governance": 4800, "evidence": 3000},
        "project_aliases": {},
    }))
    assert validate_orchestration_config_data(path)["architecture"] == "governed-memory-code-evidence"
