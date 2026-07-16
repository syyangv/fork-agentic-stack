"""Deterministic intent routing and bounded lane allocation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class LaneRequirement(Enum):
    REQUIRED = "required"
    DEFAULT = "default"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    governance: LaneRequirement
    behavioral: LaneRequirement
    evidence: LaneRequirement
    reasons: tuple[str, ...]


_SECURITY = re.compile(r"\b(security|auth(?:entication|orization)?|credential|secret|vulnerab)\w*", re.I)
_FAILURE = re.compile(r"\b(debug|failure|failed|error|retry|regression|flaky|incident)\w*", re.I)
_CODE = re.compile(r"\b(symbol|caller|callee|import|architecture|refactor|code review|function|class|handler|repository|test)\b", re.I)
_GOVERNANCE = re.compile(r"\b(permission|preference|policy|prior decision|governance)\b", re.I)
_NON_CODE = re.compile(r"\b(documentation|non-code|writing|draft|email|prose)\b", re.I)


def route_intent(intent: str, *, repo_backed: bool = True) -> RoutingDecision:
    reasons: list[str] = ["governance is always required"]
    security = bool(_SECURITY.search(intent))
    failure = bool(_FAILURE.search(intent))
    code = repo_backed and bool(_CODE.search(intent))
    governance_only_signal = bool(_GOVERNANCE.search(intent))
    non_code = bool(_NON_CODE.search(intent)) and not code

    if security:
        reasons.append("security-sensitive work requires behavioral and code evidence")
        return RoutingDecision(
            LaneRequirement.REQUIRED,
            LaneRequirement.REQUIRED,
            LaneRequirement.REQUIRED if repo_backed else LaneRequirement.OFF,
            tuple(reasons),
        )
    if failure:
        reasons.append("failure recovery requires behavioral history")
        evidence = LaneRequirement.REQUIRED if repo_backed else LaneRequirement.OFF
        return RoutingDecision(
            LaneRequirement.REQUIRED, LaneRequirement.REQUIRED, evidence, tuple(reasons)
        )
    if code:
        reasons.append("code-structural intent requires current evidence")
        return RoutingDecision(
            LaneRequirement.REQUIRED,
            LaneRequirement.DEFAULT,
            LaneRequirement.REQUIRED,
            tuple(reasons),
        )
    if governance_only_signal:
        reasons.append("governance intent does not require code evidence")
    elif non_code:
        reasons.append("non-code writing does not require code evidence")
    else:
        reasons.append("no structural code signal detected")
    return RoutingDecision(
        LaneRequirement.REQUIRED,
        LaneRequirement.DEFAULT,
        LaneRequirement.OFF,
        tuple(reasons),
    )


def allocate_lane_budgets(
    route: RoutingDecision,
    *,
    total: int = 12_000,
    reserves: dict[str, int] | None = None,
) -> dict[str, int]:
    if total <= 0:
        raise ValueError("total token budget must be positive")
    defaults = {"governance": 4_800, "behavioral": 4_200, "evidence": 3_000}
    requested = dict(reserves or defaults)
    if set(requested) != set(defaults) or any(value < 0 for value in requested.values()):
        raise ValueError("reserves must define non-negative governance, behavioral, and evidence budgets")
    reserve_total = sum(requested.values())
    if reserve_total <= 0:
        raise ValueError("at least one lane reserve must be positive")
    if reserve_total > total:
        scale = total / reserve_total
        requested = {lane: int(value * scale) for lane, value in requested.items()}

    active = {
        "governance": route.governance is not LaneRequirement.OFF,
        "behavioral": route.behavioral is not LaneRequirement.OFF,
        "evidence": route.evidence is not LaneRequirement.OFF,
    }
    allocated = {lane: value if active[lane] else 0 for lane, value in requested.items()}
    unused = total - sum(allocated.values())
    for lane in ("governance", "behavioral", "evidence"):
        if active[lane] and unused:
            allocated[lane] += unused
            unused = 0
    return allocated
