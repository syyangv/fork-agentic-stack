"""Authority-first federated memory orchestration."""
from __future__ import annotations

from .contracts import ContextPacket
from .fusion import fuse_retrieval
from .router import LaneRequirement, allocate_lane_budgets, route_intent


def build_evidence_request(provider, intent: str, *, repo_backed: bool = True):
    """Return a tool plan for structural intents without fabricating results."""
    route = route_intent(intent, repo_backed=repo_backed)
    if route.evidence is LaneRequirement.OFF:
        return None
    return provider.request_for_intent(intent)


def build_governance_packet(provider, intent: str, top_k: int = 3) -> ContextPacket:
    items, health = provider.retrieve(intent, top_k=top_k)
    warnings = list(health.get("warnings", []))
    total = sum(item.token_estimate for item in items)
    if total > 12_000:
        warnings.append("governance_budget_exceeded")
        selected = []
        used = 0
        priority = {"permission": 0, "preference": 1, "decision": 2, "lesson": 3, "review_queue": 4}
        for item in sorted(items, key=lambda value: priority.get(value.type, 99)):
            if used + item.token_estimate <= 12_000:
                selected.append(item)
                used += item.token_estimate
            else:
                warnings.append(f"governance_budget_dropped:{item.type}:{item.item_id}")
        items, total = selected, used
        health["status"] = "degraded"
        health["warnings"] = list(warnings)
    return ContextPacket(
        schema="agentic.memory.context.v1", intent=intent,
        project_id=provider.project_id,
        routing={"governance": True, "behavioral": False, "evidence": False},
        sections=(
            {"lane": "governance", "items": [item.to_dict() for item in items]},
            {"lane": "behavioral", "items": []},
            {"lane": "evidence", "items": []},
        ), warnings=tuple(warnings), health={"governance": health}, token_estimate=total,
    )


def build_shadow_packet(governance_provider, behavioral_provider, intent: str,
                        top_k: int = 3) -> ContextPacket:
    """Add observable behavioral health while suppressing prompt injection."""
    governance = build_governance_packet(governance_provider, intent, top_k=top_k)
    behavioral_items, behavioral_health = behavioral_provider.retrieve(
        intent, top_k=top_k
    )
    warnings = list(governance.warnings)
    for warning in behavioral_health.get("warnings", []):
        if warning not in warnings:
            warnings.append(warning)
    if behavioral_items:
        warnings.append("behavioral_shadow_items_suppressed")
    return ContextPacket(
        schema=governance.schema, intent=governance.intent,
        project_id=governance.project_id,
        routing={"governance": True, "behavioral": True, "evidence": False},
        sections=(
            governance.sections[0],
            {"lane": "behavioral", "items": []},
            governance.sections[2],
        ),
        warnings=tuple(warnings),
        health={
            "governance": governance.health["governance"],
            "behavioral": behavioral_health,
        },
        token_estimate=governance.token_estimate,
    )


def build_assist_packet(
    governance_provider, behavioral_provider, evidence_provider, intent: str,
    *, top_k: int = 5, total_budget: int = 12_000,
    lane_reserves: dict[str, int] | None = None,
    run_id: str | None = None, reason: str = "task_start",
) -> tuple[ContextPacket, dict]:
    """Retrieve every routed lane while preserving governance on failures."""
    route = route_intent(intent)
    budgets = allocate_lane_budgets(
        route, total=total_budget, reserves=lane_reserves,
    )
    items = {}
    health = {}
    for lane, provider in (
        ("governance", governance_provider),
        ("behavioral", behavioral_provider),
        ("evidence", evidence_provider),
    ):
        if getattr(route, lane) is LaneRequirement.OFF:
            items[lane] = []
            health[lane] = {"status": "disabled", "warnings": []}
            continue
        try:
            if lane == "behavioral":
                lane_items, lane_health = provider.retrieve(
                    intent, top_k=top_k, reason=reason, run_id=run_id,
                )
            else:
                lane_items, lane_health = provider.retrieve(intent, top_k=top_k)
        except Exception as exc:
            lane_items = []
            lane_health = {
                "status": "degraded",
                "warnings": [f"{lane}_retrieval_error:{type(exc).__name__}"],
            }
        items[lane] = lane_items
        health[lane] = lane_health
    packet, preview = fuse_retrieval(
        intent=intent, project_id=governance_provider.project_id,
        route=route, items=items, health=health, budgets=budgets,
    )
    if run_id and hasattr(behavioral_provider, "record_injected"):
        behavioral_provider.record_injected(
            run_id,
            [row["item_id"] for row in preview["selected"]],
            reason=reason,
        )
    return packet, preview


def mark_assist_blocked(packet: ContextPacket, gate_health: dict) -> ContextPacket:
    """Keep governance usable while making an unmet rollout gate visible."""
    warnings = list(packet.warnings)
    warnings.append("assist_quality_gate_blocked")
    warnings.extend(
        warning for warning in gate_health.get("warnings", [])
        if warning not in warnings
    )
    health = dict(packet.health)
    health["assist_gate"] = gate_health
    return ContextPacket(
        schema=packet.schema, intent=packet.intent, project_id=packet.project_id,
        routing=packet.routing, sections=packet.sections,
        warnings=tuple(warnings), health=health,
        token_estimate=packet.token_estimate,
    )


def format_packet_text(packet: ContextPacket) -> str:
    lines = ["Federated memory context", f"Intent: {packet.intent!r}"]
    for section in packet.sections:
        lane = section["lane"]
        lines.append(f"\n# {lane.title()} lane")
        if not section["items"]:
            lines.append(f"No {lane} records available.")
        for item in section["items"]:
            lines.append(f"\n## {item['type']} [{item['status']}] {item['item_id']}")
            lines.append(item["summary"])
            lines.append(f"Reason: {item['selection_reason']}")
    for warning in packet.warnings:
        lines.append(f"\nWARNING: {warning}")
    return "\n".join(lines)
