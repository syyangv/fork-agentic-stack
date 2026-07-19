"""Held-out acceptance accounting for the Phase 8 evolution pilot."""
from __future__ import annotations

import math
import hashlib
import json
import re
from dataclasses import dataclass
from statistics import median
from typing import Any, Mapping, Sequence


EVAL_SCHEMA = "agentic.memory.evolution-eval.v1"
_EVIDENCE_ID = re.compile(r"evi_[0-9a-f]{64}\Z")
_COST_METRICS = frozenset({"completion_seconds", "failure_recovery_steps"})


@dataclass(frozen=True, slots=True)
class EvolutionEvalResult:
    eligible: bool
    task_count: int
    baseline_success_rate: float
    assisted_success_rate: float
    relative_median_improvement: float
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EVAL_SCHEMA,
            "eligible": self.eligible,
            "task_count": self.task_count,
            "baseline_success_rate": self.baseline_success_rate,
            "assisted_success_rate": self.assisted_success_rate,
            "relative_median_improvement": self.relative_median_improvement,
            "failures": list(self.failures),
        }


def evaluate_held_out_tasks(
    rows: Sequence[Mapping[str, Any]], *,
    evidence_ledger: Mapping[str, Mapping[str, Any]],
    execution_ledger: Mapping[str, Mapping[str, Any]],
    protocol_ledger: Mapping[str, Mapping[str, Any]],
    project_id: str, repository_revision: str,
) -> EvolutionEvalResult:
    """Evaluate paired baseline/assisted observations without model judgment.

    Each task supplies executable success evidence and one positive cost metric
    (completion seconds or failure-recovery steps) for both conditions. Model
    output cannot mark a task successful and this function never changes memory
    authority state.
    """
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("held-out rows must be a sequence")
    if (not isinstance(evidence_ledger, Mapping)
            or not isinstance(execution_ledger, Mapping)
            or not isinstance(protocol_ledger, Mapping)):
        raise ValueError("evidence ledgers must be mappings")
    if not re.fullmatch(r"[0-9a-f]{40,64}", repository_revision):
        raise ValueError("repository revision is invalid")
    if not re.fullmatch(r"[0-9a-f]{16}", project_id):
        raise ValueError("project ID is invalid")
    parsed = [
        _validate_row(
            row, evidence_ledger=evidence_ledger,
            execution_ledger=execution_ledger,
            protocol_ledger=protocol_ledger,
            project_id=project_id,
            repository_revision=repository_revision,
        )
        for row in rows
    ]
    task_ids = [row["task_id"] for row in parsed]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("held-out task IDs must be distinct")
    execution_ids = [item for row in parsed for item in row.pop("_execution_ids")]
    if len(execution_ids) != len(set(execution_ids)):
        raise ValueError("held-out tasks must use globally distinct execution evidence")
    failures: list[str] = []
    if len(parsed) < 20:
        failures.append("minimum_20_held_out_tasks")
    baseline_success = sum(row["baseline_success"] for row in parsed)
    assisted_success = sum(row["assisted_success"] for row in parsed)
    count = len(parsed)
    baseline_rate = baseline_success / count if count else 0.0
    assisted_rate = assisted_success / count if count else 0.0
    if assisted_rate < baseline_rate:
        failures.append("assisted_success_regressed")
    if parsed and not all(row["skill_injected"] for row in parsed):
        failures.append("assisted_arm_missing_skill")
    improvements = [
        (row["baseline_cost"] - row["assisted_cost"]) / row["baseline_cost"]
        for row in parsed
        if row["skill_injected"] and row["baseline_success"] and row["assisted_success"]
    ]
    relative = median(improvements) if improvements else 0.0
    if not improvements:
        failures.append("no_successful_skill_assisted_pairs")
    elif relative < 0.10:
        failures.append("median_improvement_below_10_percent")
    return EvolutionEvalResult(
        eligible=not failures, task_count=count,
        baseline_success_rate=baseline_rate,
        assisted_success_rate=assisted_rate,
        relative_median_improvement=relative,
        failures=tuple(failures),
    )


def _validate_row(
    row: Mapping[str, Any], *, evidence_ledger: Mapping[str, Mapping[str, Any]],
    execution_ledger: Mapping[str, Mapping[str, Any]],
    protocol_ledger: Mapping[str, Mapping[str, Any]],
    project_id: str, repository_revision: str,
) -> dict[str, Any]:
    required = {
        "task_id", "baseline_success", "assisted_success", "baseline_cost",
        "assisted_cost", "cost_metric", "skill_injected", "baseline_evidence_id",
        "assisted_evidence_id", "protocol_digest", "skill_digest",
        "injection_evidence_id",
    }
    if not isinstance(row, Mapping) or set(row) != required:
        raise ValueError("held-out row has an unsupported shape")
    task_id = row["task_id"]
    if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
        raise ValueError("held-out task ID is invalid")
    for field in ("baseline_success", "assisted_success", "skill_injected"):
        if type(row[field]) is not bool:
            raise ValueError(f"{field} must be boolean")
    for field in ("baseline_cost", "assisted_cost"):
        value = row[field]
        if type(value) not in (int, float) or not math.isfinite(float(value)) or value <= 0:
            raise ValueError(f"{field} must be a positive finite number")
    if row["cost_metric"] not in _COST_METRICS:
        raise ValueError("cost_metric is invalid")
    for field in ("baseline_evidence_id", "assisted_evidence_id"):
        value = row[field]
        if not isinstance(value, str) or _EVIDENCE_ID.fullmatch(value) is None:
            raise ValueError(f"{field} must be an evidence ledger ID")
    if row["baseline_evidence_id"] == row["assisted_evidence_id"]:
        raise ValueError("baseline and assisted evidence must be distinct")
    for field in ("protocol_digest", "skill_digest"):
        if not isinstance(row[field], str) or re.fullmatch(r"sha256:[0-9a-f]{64}", row[field]) is None:
            raise ValueError(f"{field} is invalid")
    if (not isinstance(row["injection_evidence_id"], str)
            or _EVIDENCE_ID.fullmatch(row["injection_evidence_id"]) is None):
        raise ValueError("injection_evidence_id is invalid")
    _validate_protocol(
        protocol_ledger.get(row["protocol_digest"]), row=row,
        project_id=project_id, repository_revision=repository_revision,
    )
    _validate_injection(
        evidence_ledger.get(row["injection_evidence_id"]), row=row,
        project_id=project_id, repository_revision=repository_revision,
    )
    baseline_execution = _validate_observation(
        evidence_ledger.get(row["baseline_evidence_id"]), row=row,
        arm="baseline", repository_revision=repository_revision,
        execution_ledger=execution_ledger,
        project_id=project_id,
    )
    assisted_execution = _validate_observation(
        evidence_ledger.get(row["assisted_evidence_id"]), row=row,
        arm="assisted", repository_revision=repository_revision,
        execution_ledger=execution_ledger,
        project_id=project_id,
    )
    if baseline_execution & assisted_execution:
        raise ValueError("baseline and assisted arms must have distinct execution evidence")
    clean = dict(row)
    clean["_execution_ids"] = tuple(sorted(baseline_execution | assisted_execution))
    return clean


def _validate_observation(
    value: Any, *, row: Mapping[str, Any], arm: str, repository_revision: str,
    execution_ledger: Mapping[str, Mapping[str, Any]], project_id: str,
) -> set[str]:
    required = {
        "schema", "evidence_id", "task_id", "arm", "repository_revision",
        "executable", "success", "cost", "cost_metric", "skill_injected",
        "protocol_digest", "execution_evidence_ids",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("held-out evidence record has an unsupported shape")
    evidence_id = row[f"{arm}_evidence_id"]
    if (value["schema"] != "agentic.memory.evolution-observation.v1"
            or value["evidence_id"] != evidence_id
            or value["task_id"] != row["task_id"]
            or value["arm"] != arm
            or value["repository_revision"] != repository_revision
            or value["executable"] is not True
            or value["success"] is not row[f"{arm}_success"]
            or value["cost"] != row[f"{arm}_cost"]
            or value["cost_metric"] != row["cost_metric"]
            or value["protocol_digest"] != row["protocol_digest"]
            or value["skill_injected"] is not (row["skill_injected"] if arm == "assisted" else False)):
        raise ValueError("held-out evidence record does not match its task arm")
    execution_ids = value["execution_evidence_ids"]
    if (not isinstance(execution_ids, list) or len(execution_ids) != 1
            or len(set(execution_ids)) != len(execution_ids)
            or any(not isinstance(item, str) or _EVIDENCE_ID.fullmatch(item) is None
                   for item in execution_ids)):
        raise ValueError("held-out observation lacks executable ledger evidence")
    body = dict(value)
    body.pop("evidence_id")
    expected = "evi_" + hashlib.sha256(json.dumps(
        body, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    if evidence_id != expected:
        raise ValueError("held-out observation evidence digest is invalid")
    for execution_id in execution_ids:
        execution = execution_ledger.get(execution_id)
        provenance = execution.get("provenance") if isinstance(execution, Mapping) else None
        verification = execution.get("verification") if isinstance(execution, Mapping) else None
        locator = provenance.get("locator") if isinstance(provenance, Mapping) else None
        if (not isinstance(execution, Mapping)
                or execution.get("schema") != "agentic.memory.evidence-ledger.v1"
                or execution.get("evidence_id") != execution_id
                or not isinstance(provenance, Mapping)
                or provenance.get("source_id") != execution_id
                or provenance.get("kind") != "test_run"
                or provenance.get("provider") != "test-runner"
                or provenance.get("repository_revision") != repository_revision
                or not isinstance(verification, Mapping)
                or verification.get("executed_test") is not True
                or not isinstance(locator, Mapping)
                or locator.get("executed_test") is not True
                or type(locator.get("exit_code")) is not int
                or (locator["exit_code"] == 0) is not row[f"{arm}_success"]
                or not isinstance(locator.get("test_ids"), list)
                or f"phase8-eval:{row['task_id']}:{arm}" not in locator["test_ids"]
                or not isinstance(locator.get("command_digest"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", locator["command_digest"]) is None
                or locator.get("protocol_digest") != row["protocol_digest"]):
            raise ValueError("execution evidence is missing or not executable")
        _validate_canonical_execution(execution, execution_id, project_id=project_id)
        if arm == "assisted":
            if (locator.get("skill_injection_evidence_id") != row["injection_evidence_id"]
                    or locator.get("injected_skill_digest") != row["skill_digest"]):
                raise ValueError("assisted execution is not bound to skill injection")
        elif ("skill_injection_evidence_id" in locator
                or "injected_skill_digest" in locator):
            raise ValueError("baseline execution unexpectedly injected a skill")
        if row["cost_metric"] == "completion_seconds":
            duration_ms = locator.get("duration_ms")
            if (type(duration_ms) is not int or duration_ms <= 0
                    or not math.isclose(duration_ms / 1000, row[f"{arm}_cost"], rel_tol=0, abs_tol=1e-9)):
                raise ValueError("completion cost is not bound to measured execution duration")
        elif row["cost_metric"] == "failure_recovery_steps":
            recovery_steps = locator.get("recovery_steps")
            if type(recovery_steps) is not int or recovery_steps != row[f"{arm}_cost"]:
                raise ValueError("recovery cost is not bound to ledgered execution steps")
    return set(execution_ids)


def _validate_injection(
    value: Any, *, row: Mapping[str, Any], project_id: str, repository_revision: str,
) -> None:
    required = {
        "schema", "evidence_id", "task_id", "arm", "repository_revision",
        "protocol_digest", "skill_digest", "context_packet_digest", "provider",
        "observed_at", "project_id", "injected",
    }
    if (not isinstance(value, Mapping) or set(value) != required
            or row.get("skill_injected") is not True
            or value.get("schema") != "agentic.memory.skill-injection.v1"
            or value.get("evidence_id") != row["injection_evidence_id"]
            or value.get("task_id") != row["task_id"]
            or value.get("arm") != "assisted"
            or value.get("repository_revision") != repository_revision
            or value.get("project_id") != project_id
            or value.get("protocol_digest") != row["protocol_digest"]
            or value.get("skill_digest") != row["skill_digest"]
            or value.get("provider") != "evaluation-orchestrator"
            or not isinstance(value.get("observed_at"), str)
            or value.get("injected") is not True):
        raise ValueError("skill injection evidence is missing or mismatched")
    rebuilt = build_skill_injection_record(
        task_id=value["task_id"], project_id=project_id,
        repository_revision=repository_revision,
        protocol_digest=value["protocol_digest"], skill_digest=value["skill_digest"],
        context_packet_digest=value["context_packet_digest"],
        observed_at=value["observed_at"],
    )
    if dict(value) != rebuilt:
        raise ValueError("skill injection evidence digest is invalid")


def build_task_protocol_record(
    *, task_id: str, project_id: str, repository_revision: str,
    task_definition_digest: str, harness_digest: str,
) -> dict[str, Any]:
    body = {
        "schema": "agentic.memory.evolution-protocol.v1", "task_id": task_id,
        "project_id": project_id,
        "repository_revision": repository_revision,
        "task_definition_digest": task_definition_digest,
        "harness_digest": harness_digest,
    }
    if (not task_id or len(task_id) > 128 or not re.fullmatch(r"[0-9a-f]{16}", project_id)
            or not re.fullmatch(r"[0-9a-f]{40,64}", repository_revision)
            or any(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
                   for value in (task_definition_digest, harness_digest))):
        raise ValueError("evaluation protocol inputs are invalid")
    body["protocol_digest"] = "sha256:" + hashlib.sha256(json.dumps(
        body, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    return body


def build_skill_injection_record(
    *, task_id: str, project_id: str, repository_revision: str, protocol_digest: str,
    skill_digest: str, context_packet_digest: str, observed_at: str,
) -> dict[str, Any]:
    body = {
        "schema": "agentic.memory.skill-injection.v1", "task_id": task_id,
        "project_id": project_id,
        "arm": "assisted", "repository_revision": repository_revision,
        "protocol_digest": protocol_digest, "skill_digest": skill_digest,
        "context_packet_digest": context_packet_digest,
        "provider": "evaluation-orchestrator", "observed_at": observed_at,
        "injected": True,
    }
    if any(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None for value in (
        protocol_digest, skill_digest, context_packet_digest,
    )):
        raise ValueError("skill injection digests are invalid")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", observed_at):
        raise ValueError("skill injection timestamp is invalid")
    evidence_id = "evi_" + hashlib.sha256(json.dumps(
        body, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    return {"evidence_id": evidence_id, **body}


def _validate_protocol(
    value: Any, *, row: Mapping[str, Any], project_id: str, repository_revision: str,
) -> None:
    required = {
        "schema", "task_id", "repository_revision", "task_definition_digest",
        "harness_digest", "protocol_digest", "project_id",
    }
    if (not isinstance(value, Mapping) or set(value) != required
            or value.get("schema") != "agentic.memory.evolution-protocol.v1"
            or value.get("task_id") != row["task_id"]
            or value.get("project_id") != project_id
            or value.get("repository_revision") != repository_revision
            or value.get("protocol_digest") != row["protocol_digest"]):
        raise ValueError("held-out protocol artifact is missing or mismatched")
    rebuilt = build_task_protocol_record(
        task_id=value["task_id"], project_id=project_id,
        repository_revision=repository_revision,
        task_definition_digest=value["task_definition_digest"],
        harness_digest=value["harness_digest"],
    )
    if dict(value) != rebuilt:
        raise ValueError("held-out protocol digest is not canonical")


def _validate_canonical_execution(
    value: Mapping[str, Any], evidence_id: str, *, project_id: str,
) -> None:
    provenance = value["provenance"]
    locator = provenance["locator"]
    if (provenance.get("project_id") != project_id
            or provenance.get("source_hash") != locator["command_digest"]
            or provenance.get("observed_at") != locator.get("completed_at")
            or provenance.get("confidence") != 1.0
            or provenance.get("freshness") != "fresh"
            or value.get("verification") != {
                "repository_reconciled": True, "files_reconciled": False,
                "symbols_reconciled": False, "executed_test": True,
            }):
        raise ValueError("execution evidence is not canonical")
    seed = json.dumps({
        "kind": "test_run", "project_id": project_id,
        "revision": provenance["repository_revision"], "locator": locator,
    }, separators=(",", ":"), sort_keys=True)
    expected = "evi_" + hashlib.sha256(seed.encode()).hexdigest()
    if evidence_id != expected:
        raise ValueError("execution evidence ID is not canonical")


__all__ = [
    "EVAL_SCHEMA", "EvolutionEvalResult", "build_skill_injection_record",
    "build_task_protocol_record", "evaluate_held_out_tasks",
]
