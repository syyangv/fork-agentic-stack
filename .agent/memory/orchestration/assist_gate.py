"""Evidence-based rollout gate for behavioral context injection."""
from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ._core import SchemaValidationError, validate_schema


@dataclass(frozen=True, slots=True)
class AssistQualityGate:
    metrics: Mapping[str, Any]
    warnings: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.warnings

    @classmethod
    def from_path(
        cls, path: str | Path, *, project_id: str | None = None,
    ) -> "AssistQualityGate":
        path = Path(path)
        try:
            before = path.lstat()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if (
                stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(info.st_mode)
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
                or info.st_size > 64 * 1024
            ):
                os.close(descriptor)
                return cls({}, ("assist_metrics_unsafe_path",))
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                os.close(descriptor)
                return cls({}, ("assist_metrics_unsafe_owner",))
            if info.st_mode & 0o022:
                os.close(descriptor)
                return cls({}, ("assist_metrics_unsafe_permissions",))
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return cls({}, ("assist_metrics_missing",))
        except (json.JSONDecodeError, OSError, UnicodeError):
            return cls({}, ("assist_metrics_invalid",))
        if not isinstance(value, Mapping):
            return cls({}, ("assist_metrics_invalid",))
        return cls.from_mapping(value, project_id=project_id)

    @classmethod
    def from_mapping(
        cls, metrics: Mapping[str, Any], *, project_id: str | None = None,
    ) -> "AssistQualityGate":
        try:
            validate_schema(metrics, "assist-quality-v1.schema.json")
        except (SchemaValidationError, OSError):
            return cls(dict(metrics), ("assist_metrics_invalid",))
        warnings: list[str] = []
        if project_id is not None and metrics.get("project_id") != project_id:
            warnings.append("assist_metrics_project_mismatch")
        try:
            measured = datetime.fromisoformat(
                str(metrics.get("measured_at", "")).replace("Z", "+00:00")
            )
            if measured.tzinfo is None or measured.utcoffset() is None:
                raise ValueError
        except ValueError:
            warnings.append("assist_metrics_timestamp_invalid")
        checks = (
            (_number(metrics, "completed_episodes") >= 50, "assist_gate_episodes"),
            (_number(metrics, "task_categories") >= 5, "assist_gate_categories"),
            (0 <= _number(metrics, "duplicate_rate") < 0.05, "assist_gate_duplicates"),
            (_number(metrics, "evaluation_queries") >= 30, "assist_gate_evaluation_queries"),
            (0 <= _number(metrics, "precision_at_5") <= 1 and
             _number(metrics, "precision_at_5") >= 0.70, "assist_gate_precision"),
            (_number(metrics, "cross_project_leaks") == 0, "assist_gate_project_leakage"),
            (0 <= _number(metrics, "p95_recall_ms") < 750, "assist_gate_latency"),
        )
        for passed, warning in checks:
            if not passed:
                warnings.append(warning)
        return cls(dict(metrics), tuple(warnings))

    def health(self) -> dict[str, Any]:
        return {
            "status": "eligible" if self.eligible else "blocked",
            "eligible": self.eligible,
            "warnings": list(self.warnings),
            "metrics": dict(self.metrics),
        }


def _number(value: Mapping[str, Any], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        return float("-inf")
    number = float(item)
    return number if math.isfinite(number) else float("-inf")
