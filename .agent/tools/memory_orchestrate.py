"""CLI for provider-neutral memory orchestration."""
from __future__ import annotations

import argparse
import subprocess
import json
import os
import time
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(AGENT_ROOT / "memory"))
sys.path.insert(0, str(AGENT_ROOT / "harness"))
import scheduled_review_policy  # noqa: E402
from orchestration.identity import derive_project_identity  # noqa: E402
from orchestration import legacy_recall_baseline  # noqa: E402
from orchestration.config import load_config  # noqa: E402
from orchestration.contracts import ContractError, EventEnvelope  # noqa: E402
from orchestration.orchestrator import (  # noqa: E402
    build_governance_packet, format_packet_text,
)
from orchestration.providers.governance import GovernanceProvider  # noqa: E402
from orchestration.providers.crg_evidence import (  # noqa: E402
    CrgEvidenceProvider, EvidenceLedger,
)
from text import word_set  # noqa: E402


def _runtime_context():
    repo_root = Path(os.environ.get("AGENTIC_PROJECT_ROOT", AGENT_ROOT.parent)).resolve()
    identity = derive_project_identity(repo_root, os.environ.get("AGENTIC_GIT_REMOTE"))
    config_path = Path(os.environ.get(
        "AGENTIC_MEMORY_CONFIG", AGENT_ROOT / "memory/orchestration/config.json"
    ))
    config = load_config(config_path)
    return identity, config


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


def recall_command(
    intent: str, output_format: str, legacy: bool, top: int, *,
    run_id: str | None = None, reason: str = "task_start",
) -> str:
    del run_id, reason
    identity, config = _runtime_context()
    provider = GovernanceProvider(AGENT_ROOT, identity.project_id, word_set)
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
    evidence = _evidence_provider(identity).health()
    return {
        "schema": "agentic.memory.health.v1",
        "architecture": config.architecture,
        "project_id": identity.project_id,
        "governance": governance_health,
        "evidence": evidence,
    }


def record_command(source: str) -> dict:
    identity, _config = _runtime_context()
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
    return {
        "status": "staged-governance-candidate",
        "event_ids": [event.event_id for event in events],
    }


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


def _read_json_input(source: str, *, max_bytes: int) -> object:
    if source == "-":
        encoded = sys.stdin.buffer.read(max_bytes + 1)
    else:
        with Path(source).open("rb") as stream:
            encoded = stream.read(max_bytes + 1)
    if len(encoded) > max_bytes:
        raise ContractError(f"input exceeds {max_bytes} bytes")
    return json.loads(encoded.decode("utf-8"))


def scheduled_maintain_command() -> dict:
    """Run the existing staging-only cycle; no acceptance/provider surface."""
    environment = dict(os.environ)
    environment["AGENTIC_SCHEDULER_RESULT"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, str(AGENT_ROOT / "memory" / "auto_dream.py")],
            cwd=AGENT_ROOT, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=300, shell=False, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("scheduled maintenance failed") from exc
    if completed.returncode:
        raise RuntimeError("scheduled maintenance failed")
    if len(completed.stdout) > 4096:
        raise RuntimeError("scheduled maintenance result exceeded bound")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError("scheduled maintenance result was invalid") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"candidate_count", "rejection_count", "pending_count"}
        or any(type(result[key]) is not int or not 0 <= result[key] <= 1_000_000
               for key in result)
    ):
        raise RuntimeError("scheduled maintenance result was invalid")
    return {
        "status": "staged",
        "authority": "no_auto_accept",
        "candidate_count": result["candidate_count"],
        "rejection_count": result["rejection_count"],
    }


def scheduled_review_prepare_command() -> dict:
    """Prepare a body-free, bounded review snapshot; delivery remains deferred."""
    queue = AGENT_ROOT / "memory" / "working" / "REVIEW_QUEUE.md"
    maintenance_state = AGENT_ROOT / "memory" / "dream-state.json"
    local_config = AGENT_ROOT / "memory" / "orchestration" / "scheduled-local.json"
    return scheduled_review_policy.prepare_review_snapshot(
        queue, maintenance_state, local_config,
    )


def _scheduled_with_health(label: str, command):
    """Record only bounded operational metadata for generated LaunchAgent runs."""
    if os.environ.get("AGENTIC_SCHEDULER_RUN") != "1":
        return command()
    from orchestration.scheduler_health import SchedulerHealthStore
    store = SchedulerHealthStore(AGENT_ROOT)
    revision = os.environ.get("AGENTIC_SOURCE_REVISION", "unknown")
    running = store.start(label, tool_version="memory_orchestrate.v1", source_revision=revision)
    run_token = running["run_token"]
    started = time.monotonic()
    try:
        value = command()
    except BaseException:
        store.finish(label, run_token=run_token, success=False,
                     duration_ms=int((time.monotonic() - started) * 1000))
        raise
    notification = value.get("notification") if isinstance(value, dict) else None
    outcome = "deferred" if notification == "requested_deferred" else "not_requested"
    store.finish(
        label, run_token=run_token, success=True,
        duration_ms=int((time.monotonic() - started) * 1000),
        candidate_count=value.get("candidate_count") if isinstance(value, dict) else None,
        rejection_count=value.get("rejection_count") if isinstance(value, dict) else None,
        notification=outcome,
    )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Governed Memory + Code Evidence")
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
    maintain = sub.add_parser("maintain")
    maintain.add_argument("--stage-candidates", action="store_true", required=True)
    maintain.add_argument("--scheduled", action="store_true", required=True)
    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    prepare = review_sub.add_parser("prepare")
    prepare.add_argument("--scheduled", action="store_true", required=True)
    prepare.add_argument("--notify", action="store_true", required=True)
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
        elif args.command == "maintain":
            print(json.dumps(_scheduled_with_health(
                "com.syang.agentic-stack.auto-dream", scheduled_maintain_command,
            ), indent=2))
        elif args.command == "review":
            print(json.dumps(_scheduled_with_health(
                "com.syang.agentic-stack.review-notify", scheduled_review_prepare_command,
            ), indent=2))
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
        ContractError, json.JSONDecodeError, OSError, RuntimeError, subprocess.TimeoutExpired,
        UnicodeError, ValueError,
    ) as exc:
        print(json.dumps({
            "status": "error", "error": type(exc).__name__, "message": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
