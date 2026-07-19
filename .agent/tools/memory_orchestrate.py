"""CLI for provider-neutral memory orchestration."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "memory"))
sys.path.insert(0, str(AGENT_ROOT / "harness"))
from orchestration.identity import derive_project_identity  # noqa: E402
from orchestration._core import contains_sensitive_plaintext  # noqa: E402
from orchestration import legacy_recall_baseline  # noqa: E402
from orchestration.assist_gate import AssistQualityGate  # noqa: E402
from orchestration.config import load_config  # noqa: E402
from orchestration.contracts import ContractError, EventEnvelope  # noqa: E402
from orchestration.memos_factory import create_memos_provider  # noqa: E402
from orchestration.orchestrator import (  # noqa: E402
    build_assist_packet, build_governance_packet, build_shadow_packet,
    format_packet_text, mark_assist_blocked,
)
from orchestration.providers.governance import GovernanceProvider  # noqa: E402
from orchestration.providers.crg_evidence import (  # noqa: E402
    CrgEvidenceProvider, EvidenceLedger,
)
from orchestration.revalidation import record_retrieval_outcome  # noqa: E402
from orchestration.promotion import stage_behavioral_candidates  # noqa: E402
from text import word_set  # noqa: E402


def _runtime_context():
    repo_root = Path(os.environ.get("AGENTIC_PROJECT_ROOT", AGENT_ROOT.parent)).resolve()
    identity = derive_project_identity(repo_root, os.environ.get("AGENTIC_GIT_REMOTE"))
    config_path = Path(os.environ.get(
        "AGENTIC_MEMORY_CONFIG", AGENT_ROOT / "memory/orchestration/config.json"
    ))
    config = load_config(config_path)
    return identity, config


def _assist_gate(identity) -> AssistQualityGate:
    data_root = Path(os.environ.get(
        "AGENTIC_MEMOS_DATA_ROOT", AGENT_ROOT / "runtime" / "memos",
    ))
    path = Path(os.environ.get(
        "AGENTIC_ASSIST_METRICS",
        data_root / identity.project_id / "assist-quality.json",
    ))
    return AssistQualityGate.from_path(path, project_id=identity.project_id)


def _provider_session(identity, mode: str, *, assist_deadline: float | None = None):
    return create_memos_provider(
        AGENT_ROOT,
        identity.project_id,
        mode=mode,
        code_root=os.environ.get("AGENTIC_MEMOS_CODE_ROOT"),
        data_root=os.environ.get("AGENTIC_MEMOS_DATA_ROOT"),
        repo_root=identity.repo_root,
        assist_deadline=assist_deadline,
    )


def _evidence_provider(identity) -> CrgEvidenceProvider:
    registry = os.environ.get("AGENTIC_CRG_REGISTRY")
    ledger_path = Path(os.environ.get(
        "AGENTIC_EVIDENCE_LEDGER",
        AGENT_ROOT / "memory" / "evidence" / "ledger.jsonl",
    ))
    return CrgEvidenceProvider(
        repo_root=identity.repo_root, project_id=identity.project_id,
        registry_path=registry, ledger=EvidenceLedger(ledger_path),
    )


class _UnavailableBehavioralProvider:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def retrieve(self, *_args, **_kwargs):
        return [], {
            "status": "degraded", "mode": "assist",
            "warnings": [
                "behavioral_unavailable",
                f"behavioral_provider_error:{type(self.error).__name__}",
            ],
        }


def recall_command(
    intent: str, output_format: str, legacy: bool, top: int, *,
    run_id: str | None = None, reason: str = "task_start",
) -> str:
    if run_id is not None and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id)
        or contains_sensitive_plaintext(run_id)
    ):
        raise ValueError("run-id must be a non-sensitive opaque identifier")
    identity, config = _runtime_context()
    provider = GovernanceProvider(AGENT_ROOT, identity.project_id, word_set)
    preview = None
    if config.mode == "assist":
        gate = _assist_gate(identity)
        if gate.eligible:
            deadline = time.monotonic() + 0.7
            try:
                session = _provider_session(
                    identity, "assist", assist_deadline=deadline,
                )
                with session as behavioral:
                    packet, preview = build_assist_packet(
                        provider, behavioral, _evidence_provider(identity), intent,
                        top_k=top, total_budget=config.total_token_budget,
                        lane_reserves=dict(config.lane_reserves),
                        run_id=run_id, reason=reason,
                    )
            except Exception as exc:
                packet, preview = build_assist_packet(
                    provider, _UnavailableBehavioralProvider(exc),
                    _evidence_provider(identity), intent,
                    top_k=top, total_budget=config.total_token_budget,
                    lane_reserves=dict(config.lane_reserves),
                    run_id=run_id, reason=reason,
                )
        else:
            packet = mark_assist_blocked(
                build_governance_packet(provider, intent, top_k=top), gate.health(),
            )
    elif config.mode == "shadow":
        with _provider_session(identity, "shadow") as behavioral:
            packet = build_shadow_packet(provider, behavioral, intent, top_k=top)
    else:
        packet = build_governance_packet(provider, intent, top_k=top)
    comparison = None
    if legacy:
        result, meta = legacy_recall_baseline.recall(
            intent, AGENT_ROOT / "memory/semantic/lessons.jsonl",
            AGENT_ROOT / "memory/semantic/LESSONS.md", word_set, top_k=top,
        )
        comparison = {"result": result, "meta": meta,
                      "text": legacy_recall_baseline.format_pretty(intent, result, meta)}
    if output_format == "json":
        payload = {"context_packet": packet.to_dict()}
        if preview is not None:
            payload["retrieval_preview"] = preview
        if comparison is not None:
            payload["legacy"] = comparison
        return json.dumps(payload, indent=2, ensure_ascii=False)
    text = format_packet_text(packet)
    if comparison is not None:
        text += "\n\n--- legacy comparison ---\n" + comparison["text"]
    return text


def health_command() -> dict:
    identity, config = _runtime_context()
    governance = GovernanceProvider(AGENT_ROOT, identity.project_id, word_set)
    _, governance_health = governance.retrieve("orchestration health", top_k=0)
    behavioral = {
        "status": "disabled", "mode": "off", "warnings": [],
    }
    gate = _assist_gate(identity) if config.mode == "assist" else None
    effective_mode = (
        "assist" if gate is not None and gate.eligible else
        "shadow" if config.mode in {"shadow", "assist"} else "off"
    )
    if effective_mode != "off":
        with _provider_session(identity, effective_mode) as provider:
            behavioral = provider.health()
    if gate is not None:
        behavioral = {**behavioral, "assist_gate": gate.health(),
                      "effective_mode": effective_mode}
    evidence = _evidence_provider(identity).health()
    return {
        "schema": "agentic.memory.health.v1",
        "mode": config.mode,
        "project_id": identity.project_id,
        "governance": governance_health,
        "behavioral": behavioral,
        "evidence": evidence,
    }


def record_command(source: str) -> dict:
    identity, config = _runtime_context()
    if source == "-":
        encoded = sys.stdin.buffer.read(1024 * 1024 + 1)
    else:
        with Path(source).open("rb") as stream:
            encoded = stream.read(1024 * 1024 + 1)
    if len(encoded) > 1024 * 1024:
        raise ContractError("event input exceeds 1 MiB")
    raw = encoded.decode("utf-8")
    parsed = json.loads(raw)
    values = parsed if isinstance(parsed, list) else [parsed]
    if not values or len(values) > 100 or any(not isinstance(item, dict) for item in values):
        raise ContractError("event input must be an object or a non-empty array of at most 100 objects")
    events = [EventEnvelope.from_external(item) for item in values]
    if any(event.project_id != identity.project_id for event in events):
        raise ContractError("event project does not match the active project")
    if config.mode == "off":
        return {
            "status": "disabled", "mode": "off",
            "event_ids": [event.event_id for event in events],
        }
    mode = config.mode
    gate = _assist_gate(identity) if mode == "assist" else None
    if gate is not None and not gate.eligible:
        mode = "shadow"
    with _provider_session(identity, mode) as provider:
        results = [provider.record(event) for event in events]
        health = provider.health()
    for event in events:
        record_retrieval_outcome(AGENT_ROOT, event)
    if gate is not None:
        health = {**health, "assist_gate": gate.health(), "effective_mode": mode}
    totals = {
        name: sum(result[name] for result in results)
        for name in ("enqueued", "delivered", "ambiguous", "dead")
    }
    return {
        "status": "recorded",
        "event_ids": [event.event_id for event in events],
        **totals,
        "retrieved": sum(result.get("retrieved", 0) for result in results),
        "health": health,
    }


def export_command(limit: int, max_bytes: int) -> dict:
    identity, config = _runtime_context()
    if config.mode not in {"shadow", "assist"}:
        raise RuntimeError("behavioral export requires shadow or assist mode")
    if not 1 <= limit <= 100:
        raise ValueError("export limit must be between 1 and 100")
    if not 256 <= max_bytes <= 1024 * 1024:
        raise ValueError("export max-bytes must be between 256 and 1048576")
    with _provider_session(identity, config.mode) as provider:
        return provider.export_shadow(limit=limit, max_bytes=max_bytes)


def evidence_health_command() -> dict:
    identity, _config = _runtime_context()
    return _evidence_provider(identity).health()


def evidence_request_command(
    operation: str, query: str, target: str, intent: str,
) -> dict:
    identity, _config = _runtime_context()
    provider = _evidence_provider(identity)
    if operation == "auto":
        if not intent:
            raise ValueError("automatic evidence requests require --intent")
        return provider.request_for_intent(intent)
    return provider.request(
        operation=operation, query=query, target=target,
    )


def evidence_record_command(source: str, *, test_run: bool = False) -> dict:
    identity, _config = _runtime_context()
    value = _read_json_input(source, max_bytes=64 * 1024)
    if not isinstance(value, dict):
        raise ContractError("evidence input must be one JSON object")
    provider = _evidence_provider(identity)
    return provider.record_test_run(value) if test_run else provider.record(value)


def candidates_command(intent: str, top: int, stage: bool) -> dict:
    identity, _config = _runtime_context()
    try:
        with _provider_session(identity, "assist") as provider:
            candidates, health = provider.discover_candidates(intent, top_k=top)
    except Exception as exc:
        return {
            "status": "degraded", "candidates": [], "staged": 0,
            "health": {"status": "degraded", "warnings": [
                "behavioral_unavailable",
                f"behavioral_provider_error:{type(exc).__name__}",
            ]},
        }
    staged = stage_behavioral_candidates(
        candidates, AGENT_ROOT / "memory/candidates",
    ) if stage else 0
    return {
        "status": "staged" if stage else "preview",
        "candidates": candidates, "staged": staged, "health": health,
    }


def _read_json_input(source: str, *, max_bytes: int) -> object:
    if source == "-":
        encoded = sys.stdin.buffer.read(max_bytes + 1)
    else:
        with Path(source).open("rb") as stream:
            encoded = stream.read(max_bytes + 1)
    if len(encoded) > max_bytes:
        raise ContractError(f"input exceeds {max_bytes} bytes")
    return json.loads(encoded.decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Federated memory orchestration")
    sub = parser.add_subparsers(dest="command", required=True)
    recall = sub.add_parser("recall")
    recall.add_argument("--intent", required=True)
    recall.add_argument("--format", choices=("json", "text"), default="text")
    recall.add_argument("--legacy", action="store_true")
    recall.add_argument("--top", type=int, default=3)
    recall.add_argument("--run-id")
    recall.add_argument("--reason", choices=(
        "task_start", "decision_point", "recovery", "user_feedback", "completion",
    ), default="task_start")
    sub.add_parser("health")
    record = sub.add_parser("record", help="validate and deliver an EventEnvelope")
    record.add_argument("--event", default="-", help="JSON file or - for stdin")
    export = sub.add_parser("export-shadow", help="bounded redacted behavioral export")
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--max-bytes", type=int, default=64 * 1024)
    candidates = sub.add_parser("candidates", help="preview bridge-observable MemOS candidates")
    candidates.add_argument("--intent", required=True)
    candidates.add_argument("--top", type=int, default=20)
    candidates.add_argument("--stage", action="store_true")
    evidence = sub.add_parser("evidence", help="plan and record revision-bound code evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_sub.add_parser("health")
    request = evidence_sub.add_parser("request")
    request.add_argument("--operation", default="auto", choices=(
        "auto", "semantic_search", "graph_query", "impact", "architecture", "change_review",
    ))
    request.add_argument("--intent", default="")
    request.add_argument("--query", default="")
    request.add_argument("--target", default="")
    evidence_record = evidence_sub.add_parser("record")
    evidence_record.add_argument("--input", default="-")
    test_record = evidence_sub.add_parser("record-test")
    test_record.add_argument("--input", default="-")
    args = parser.parse_args()
    try:
        if args.command == "recall":
            print(recall_command(
                args.intent, args.format, args.legacy, args.top,
                run_id=args.run_id, reason=args.reason,
            ))
        elif args.command == "health":
            print(json.dumps(health_command(), indent=2, ensure_ascii=False))
        elif args.command == "record":
            print(json.dumps(record_command(args.event), indent=2, ensure_ascii=False))
        elif args.command == "export-shadow":
            print(json.dumps(
                export_command(args.limit, args.max_bytes), indent=2, ensure_ascii=False,
            ))
        elif args.command == "candidates":
            print(json.dumps(
                candidates_command(args.intent, args.top, args.stage),
                indent=2, ensure_ascii=False,
            ))
        elif args.command == "evidence":
            if args.evidence_command == "health":
                value = evidence_health_command()
            elif args.evidence_command == "request":
                value = evidence_request_command(
                    args.operation, args.query, args.target, args.intent,
                )
            elif args.evidence_command == "record":
                value = evidence_record_command(args.input)
            else:
                value = evidence_record_command(args.input, test_run=True)
            print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    except (
        ContractError, json.JSONDecodeError, OSError, RuntimeError,
        UnicodeError, ValueError,
    ) as exc:
        print(json.dumps({
            "status": "error", "error": type(exc).__name__, "message": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
