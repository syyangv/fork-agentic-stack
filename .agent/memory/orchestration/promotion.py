"""Translate bridge-observable MemOS hypotheses into review-only candidates."""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from candidate_lock import atomic_write_json, candidate_lifecycle_lock

from ._core import contains_sensitive_plaintext, validate_schema


class PromotionError(ValueError):
    pass


def translate_memos_record(
    kind: str, record: Mapping[str, Any], project_id: str,
    *, observed_at: str | None = None,
) -> dict[str, Any]:
    """Create a staged candidate; upstream active never means accepted here."""
    normalized = str(kind).strip().lower().replace("-", "_")
    if normalized not in {"policy", "world_model", "skill", "decision_repair"}:
        raise PromotionError("unsupported MemOS candidate kind")
    if normalized == "decision_repair" and record.get("captureSource") != "normalized_event":
        raise PromotionError(
            "raw decision repairs are not bridge-observable; a normalized event is required"
        )
    if not _owned(record, project_id):
        raise PromotionError("MemOS candidate is unowned or cross-project")
    share = record.get("share")
    if record.get("shareScope") == "hub" or (
        isinstance(share, Mapping) and share.get("scope") == "hub"
    ):
        raise PromotionError("hub-shared hypotheses cannot become candidates")
    if contains_sensitive_plaintext(record):
        raise PromotionError("MemOS candidate contains sensitive plaintext")
    source_id = _text(record.get("id"), 512)
    if not source_id:
        raise PromotionError("MemOS candidate has no provider identity")

    source_kind = normalized
    provider_key = f"{normalized}_id"
    if normalized == "policy" and str(record.get("experienceType", "")).startswith("repair_"):
        provider_key = "policy_id"
    claim = _claim(normalized, record)
    if len(claim) < 20:
        raise PromotionError("MemOS candidate claim is too short")
    evidence_refs = _evidence_refs(normalized, record)
    conditions = _conditions(normalized, record)
    support = _optional_int(record.get("support"))
    gain = _optional_float(record.get("gain"), signed=True)
    trials = _optional_int(record.get("trialsAttempted")) if normalized == "skill" else None
    trial_passes = _optional_int(record.get("trialsPassed")) if normalized == "skill" else None
    upstream_status = str(record.get("status", "")).lower()
    freshness = "stale" if upstream_status in {"archived", "retired", "stale"} else "fresh"
    code_refs = _code_refs(record.get("codeRefs", record.get("code_refs", [])))
    now = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    digest = hashlib.sha256(
        f"{project_id}\0{source_kind}\0{source_id}".encode("utf-8")
    ).hexdigest()[:24]
    candidate = {
        "schema": "agentic.memory.candidate.v1",
        "id": f"memos_{source_kind}_{digest}",
        "claim": claim[:4000], "conditions": conditions,
        "status": "staged", "source_layer": "behavioral",
        "source_kind": source_kind, "provider_ids": {provider_key: source_id},
        "target_kind": {
            "policy": "governance_lesson", "world_model": "domain_knowledge",
            "skill": "skill_guidance", "decision_repair": "decision_rule",
        }[source_kind],
        "project_scope": {"project_id": project_id},
        "evidence_refs": evidence_refs, "support": support, "gain": gain,
        "trial_count": trials, "trial_pass_count": trial_passes,
        "freshness": freshness,
        "code_specific": bool(record.get("codeSpecific") is True or code_refs),
        "code_refs": code_refs, "staged_at": now,
        "decisions": [{"ts": now, "action": "staged", "reviewer": "memos-local"}],
        "support_count": 0, "contradiction_count": 0,
        "outcome_history": [], "rejection_count": 0,
    }
    validate_schema(candidate, "candidate-v1.schema.json")
    return candidate


def stage_behavioral_candidates(
    candidates: Sequence[Mapping[str, Any]], candidates_dir: str | Path,
) -> int:
    """Stage safely while preserving human rejection and accepted terminal state."""
    root = Path(candidates_dir)
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    with candidate_lifecycle_lock(str(root)):
        for raw in candidates:
            candidate = dict(raw)
            validate_schema(candidate, "candidate-v1.schema.json")
            if contains_sensitive_plaintext(candidate):
                raise PromotionError("candidate contains sensitive plaintext")
            candidate_id = candidate["id"]
            rejected = root / "rejected" / f"{candidate_id}.json"
            deferred = root / "deferred" / f"{candidate_id}.json"
            graduated = root / "graduated" / f"{candidate_id}.json"
            path = root / f"{candidate_id}.json"
            locations = [
                value for value in (path, deferred, rejected, graduated)
                if value.is_file()
            ]
            if len(locations) > 1:
                raise PromotionError(
                    "candidate exists in multiple lifecycle locations"
                )
            if deferred.is_file():
                continue
            if rejected.is_file():
                prior = _load(rejected)
                decisions = prior.get("decisions", [])
                latest_reject = next((row for row in reversed(decisions)
                                      if row.get("action") == "rejected"), {})
                if latest_reject.get("reviewer") not in {
                    "heuristic_prefilter", "scheduled-deterministic-triage",
                }:
                    continue
                rejected.replace(root / f"{candidate_id}.json")
            if graduated.is_file():
                continue
            if path.is_file():
                prior = _load(path)
                candidate["decisions"] = [
                    *prior.get("decisions", []),
                    {"ts": candidate["staged_at"], "action": "restaged",
                     "reviewer": "memos-local"},
                ]
                candidate["support_count"] = prior.get("support_count", 0)
                candidate["contradiction_count"] = prior.get("contradiction_count", 0)
                candidate["outcome_history"] = prior.get("outcome_history", [])
                candidate["rejection_count"] = prior.get("rejection_count", 0)
            atomic_write_json(str(path), candidate)
            from .revalidation import RevalidationIndex
            RevalidationIndex(
                root.parent / "evidence" / "revalidation.sqlite3"
            ).link_candidate(candidate)
            written += 1
    return written


def _owned(record: Mapping[str, Any], project_id: str) -> bool:
    return (
        record.get("ownerAgentKind") == "hermes"
        and record.get("ownerProfileId") == project_id
        and record.get("ownerWorkspaceId") == project_id
    )


def _claim(kind: str, row: Mapping[str, Any]) -> str:
    if kind == "policy":
        pieces = [row.get(name) for name in (
            "title", "trigger", "procedure", "verification", "boundary",
        )]
        preferences = [*(_strings(row.get("preference"))), *(_strings(row.get("antiPattern")))]
        pieces.extend(preferences)
    elif kind == "world_model":
        pieces = [row.get("title"), row.get("body")]
        structure = row.get("structure")
        if isinstance(structure, Mapping):
            for section in ("environment", "inference", "constraints"):
                for value in structure.get(section, []) if isinstance(structure.get(section, []), list) else []:
                    if isinstance(value, Mapping):
                        pieces.extend((value.get("label"), value.get("description")))
    elif kind == "skill":
        pieces = [row.get("name"), row.get("invocationGuide")]
        guidance = row.get("decisionGuidance")
        if isinstance(guidance, Mapping):
            pieces.extend(_strings(guidance.get("preference")))
            pieces.extend(_strings(guidance.get("antiPattern")))
    else:
        pieces = [row.get("preference"), row.get("antiPattern")]
    return " ".join(_text(value, 1000) for value in pieces if _text(value, 1000)).strip()


def _evidence_refs(kind: str, row: Mapping[str, Any]) -> list[str]:
    fields = {
        "policy": ("sourceEpisodeIds", "sourceFeedbackIds", "sourceTraceIds"),
        "world_model": ("policyIds",),
        "skill": ("evidenceAnchors", "sourcePolicyIds", "sourceWorldModelIds"),
        "decision_repair": ("highValueTraceIds", "lowValueTraceIds"),
    }[kind]
    values: list[str] = []
    for field in fields:
        for value in _strings(row.get(field)):
            if value not in values:
                values.append(value[:128])
    if kind == "world_model":
        structure = row.get("structure")
        if isinstance(structure, Mapping):
            for section in ("environment", "inference", "constraints"):
                for entry in structure.get(section, []) if isinstance(structure.get(section, []), list) else []:
                    if isinstance(entry, Mapping):
                        for value in _strings(entry.get("evidenceIds")):
                            if value not in values:
                                values.append(value[:128])
    return values[:100]


def _conditions(kind: str, row: Mapping[str, Any]) -> list[str]:
    values = [kind, row.get("experienceType"), row.get("trigger"), row.get("name")]
    return list(dict.fromkeys(
        token for value in values for token in _text(value, 500).lower().replace("_", " ").split()
        if 2 <= len(token) <= 100
    ))[:50]


def _code_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for row in value[:50]:
        if not isinstance(row, Mapping):
            continue
        path = _text(row.get("file_path", row.get("path")), 500)
        name = _text(row.get("qualified_name", row.get("symbol")), 500)
        if path and name:
            result.append({"file_path": path, "qualified_name": name})
    return result


def _strings(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [_text(item, 512) for item in values if _text(item, 512)]


def _text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return max(0, min(1_000_000, int(number))) if math.isfinite(number) else None


def _optional_float(value: Any, *, signed: bool) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(-1.0 if signed else 0.0, min(1.0, number))


def _load(path: Path) -> dict[str, Any]:
    value = __import__("json").loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PromotionError("candidate lifecycle record must be an object")
    return value
