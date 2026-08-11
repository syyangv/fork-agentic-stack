"""Authority-first governed memory and code evidence orchestration."""
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
        routing={"governance": True, "evidence": False},
        sections=(
            {"lane": "governance", "items": [item.to_dict() for item in items]},
            {"lane": "evidence", "items": []},
        ), warnings=tuple(warnings), health={"governance": health}, token_estimate=total,
    )


def build_context_packet(
    governance_provider, evidence_provider, intent: str,
    *, top_k: int = 5, total_budget: int = 7_800,
    lane_reserves: dict[str, int] | None = None,
) -> tuple[ContextPacket, dict]:
    """Retrieve governed memory and optional current code evidence."""
    route = route_intent(intent)
    budgets = allocate_lane_budgets(
        route, total=total_budget, reserves=lane_reserves,
    )
    items = {}
    health = {}
    for lane, provider in (
        ("governance", governance_provider),
        ("evidence", evidence_provider),
    ):
        if getattr(route, lane) is LaneRequirement.OFF:
            items[lane] = []
            health[lane] = {"status": "disabled", "warnings": []}
            continue
        try:
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
    return packet, preview


def format_packet_text(packet: ContextPacket) -> str:
    lines = ["Governed memory and code evidence", f"Intent: {packet.intent!r}"]
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
