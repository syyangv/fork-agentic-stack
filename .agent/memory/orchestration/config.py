"""Strict local configuration for the memory orchestrator."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._core import SchemaValidationError, deep_freeze, thaw, validate_schema


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryOrchestrationConfig:
    mode: str = "off"
    total_token_budget: int = 12_000
    lane_reserves: Mapping[str, int] = field(
        default_factory=lambda: {
            "governance": 4_800,
            "behavioral": 4_200,
            "evidence": 3_000,
        }
    )
    project_aliases: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane_reserves", deep_freeze(self.lane_reserves))
        object.__setattr__(self, "project_aliases", deep_freeze(self.project_aliases))
        if sum(self.lane_reserves.values()) != self.total_token_budget:
            raise ConfigError("lane reserves must sum to total_token_budget")

    @classmethod
    def from_external(cls, data: Mapping[str, Any]) -> "MemoryOrchestrationConfig":
        merged = {
            "mode": "off",
            "total_token_budget": 12_000,
            "lane_reserves": {
                "governance": 4_800,
                "behavioral": 4_200,
                "evidence": 3_000,
            },
            "project_aliases": {},
            **thaw(data),
        }
        try:
            validate_schema(merged, "orchestration-config-v1.schema.json")
            return cls(**merged)
        except (SchemaValidationError, TypeError, ConfigError, OSError, ValueError) as exc:
            raise ConfigError(str(exc)) from exc


def load_config(path: str | Path) -> MemoryOrchestrationConfig:
    config_path = Path(path)
    if not config_path.exists():
        return MemoryOrchestrationConfig()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read orchestration config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("orchestration config must be a JSON object")
    return MemoryOrchestrationConfig.from_external(raw)
