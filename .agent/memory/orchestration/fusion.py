"""Authority-preserving lane-local ranking, deduplication, and previews."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ._core import validate_schema
from .contracts import ContextPacket, RetrievalItem
from .router import RoutingDecision


LANES = ("governance", "behavioral", "evidence")
PREVIEW_CATEGORIES = ("selected", "rejected", "deduplicated", "stale", "over_budget")
MAX_PREVIEW_ITEMS = 100
_STATUS_PRIORITY = {
    "accepted": 0, "active": 1, "probationary": 2,
    "fresh": 3, "raw": 4, "stale": 99,
}


def fuse_retrieval(
    *, intent: str, project_id: str, route: RoutingDecision,
    items: Mapping[str, Sequence[RetrievalItem]],
    health: Mapping[str, Mapping[str, Any]], budgets: Mapping[str, int],
) -> tuple[ContextPacket, dict[str, Any]]:
    if set(budgets) != set(LANES) or any(value < 0 for value in budgets.values()):
        raise ValueError("fusion budgets must define non-negative values for every lane")
    preview: dict[str, Any] = {category: [] for category in PREVIEW_CATEGORIES}
    category_counts = {category: 0 for category in PREVIEW_CATEGORIES}
    truncated: list[str] = []

    def observe(category: str, descriptor: dict[str, str]) -> None:
        category_counts[category] += 1
        if len(preview[category]) < MAX_PREVIEW_ITEMS:
            preview[category].append(descriptor)
        elif category not in truncated:
            truncated.append(category)
    selected: dict[str, list[RetrievalItem]] = {lane: [] for lane in LANES}
    seen: set[str] = set()
    warnings: list[str] = []
    used_total = 0

    for lane in LANES:
        lane_used = 0
        ranked = sorted(
            items.get(lane, ()),
            key=lambda row: (
                _STATUS_PRIORITY.get(row.status, 50),
                -row.provider_score, row.item_id,
            ),
        )
        for row in ranked:
            descriptor = {"lane": lane, "item_id": row.item_id[:512]}
            if row.scope.get("project_id") != project_id or row.lane != lane:
                observe("rejected", {**descriptor, "reason": "scope_or_lane_mismatch"})
                continue
            if row.status == "stale" or any(
                ref.get("freshness") == "stale" for ref in row.provenance
            ):
                observe("stale", {**descriptor, "reason": "stale_provenance"})
                continue
            fingerprint = _fingerprint(row.summary)
            if fingerprint in seen:
                observe("deduplicated", {**descriptor, "reason": "higher_authority_duplicate"})
                continue
            if category_counts["selected"] >= MAX_PREVIEW_ITEMS:
                observe("over_budget", {**descriptor, "reason": "item_count_limit"})
                continue
            if lane_used + row.token_estimate > budgets[lane]:
                observe("over_budget", {**descriptor, "reason": "lane_budget"})
                continue
            selected[lane].append(row)
            lane_used += row.token_estimate
            used_total += row.token_estimate
            seen.add(fingerprint)
            observe("selected", {**descriptor, "reason": row.selection_reason[:500]})
        if items.get(lane) and not selected[lane]:
            warnings.append(f"{lane}_items_suppressed")
        lane_health = health.get(lane, {})
        for warning in lane_health.get("warnings", []):
            if warning not in warnings:
                warnings.append(str(warning))

    packet = ContextPacket(
        schema="agentic.memory.context.v1", intent=intent, project_id=project_id,
        routing={
            "governance": route.governance.value != "off",
            "behavioral": route.behavioral.value != "off",
            "evidence": route.evidence.value != "off",
        },
        sections=tuple({
            "lane": lane, "items": [row.to_dict() for row in selected[lane]],
        } for lane in LANES),
        warnings=tuple(warnings), health=dict(health), token_estimate=used_total,
    )
    result = {
        "schema": "agentic.memory.retrieval-preview.v1",
        **preview,
        "category_counts": category_counts,
        "truncated": truncated,
        "token_estimate": used_total,
        "lane_budgets": dict(budgets),
    }
    validate_schema(result, "retrieval-preview-v1.schema.json")
    return packet, result


def _fingerprint(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
