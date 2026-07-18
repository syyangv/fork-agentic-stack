"""Governance-only shell for the federated memory orchestrator."""
from __future__ import annotations

from .contracts import ContextPacket


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


def format_packet_text(packet: ContextPacket) -> str:
    lines = ["Governance memory context", f"Intent: {packet.intent!r}"]
    governance = packet.sections[0]["items"]
    if not governance:
        lines.append("No governance records available.")
    for item in governance:
        lines.append(f"\n## {item['type']} [{item['status']}] {item['item_id']}")
        lines.append(item["summary"])
        lines.append(f"Reason: {item['selection_reason']}")
    for warning in packet.warnings:
        lines.append(f"\nWARNING: {warning}")
    return "\n".join(lines)
