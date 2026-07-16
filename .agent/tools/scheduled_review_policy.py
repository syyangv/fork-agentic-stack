"""Deterministic scheduled triage that can reject noise but never accept."""
from __future__ import annotations

import re
from dataclasses import dataclass


NOISE_PATTERNS = (
    r"Wrote .*(settings\.json|WORKSPACE\.md|REVIEW_QUEUE\.md|AGENT_LEARNINGS\.jsonl)",
    r"Patched .*(WORKSPACE\.md|REVIEW_QUEUE\.md)",
    r"^(Patched|Edited) .+ \(\+\d+/-\d+ lines\)$",
    r"^Wrote (?!.*(LESSONS\.md|DOMAIN_KNOWLEDGE\.md|DECISIONS\.md|permissions\.md)).+ \(\d+ lines\)$",
    r"Tool \w+ completed (successfully|with failure)",
    r"^(Ran|bash): .*\.(json|jsonl|plist)$",
    r"Edited .*(\.claude/projects|\.agent/memory).*/.*: replaced",
    r"High-stakes op completed \((prod|staging|deploy|production)\):",
    r"^Ran: ",
)
_NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True)
class TriageDecision:
    needs_review: list[dict]
    rejected: list[dict]


def triage_candidates(candidates: list[dict]) -> TriageDecision:
    """Return deterministic junk rejections and everything requiring a human."""
    needs_review, rejected = [], []
    for candidate in candidates:
        if _NOISE_RE.search(candidate.get("claim", "")):
            rejected.append(candidate)
        else:
            needs_review.append(candidate)
    return TriageDecision(needs_review=needs_review, rejected=rejected)
