"""CLI for provider-neutral memory orchestration."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "memory"))
sys.path.insert(0, str(AGENT_ROOT / "harness"))
from orchestration.identity import derive_project_identity  # noqa: E402
from orchestration import legacy_recall_baseline  # noqa: E402
from orchestration.config import load_config  # noqa: E402
from orchestration.contracts import ContractError, EventEnvelope  # noqa: E402
from orchestration.memos_factory import create_memos_provider  # noqa: E402
from orchestration.orchestrator import (  # noqa: E402
    build_governance_packet, build_shadow_packet, format_packet_text,
)
from orchestration.providers.governance import GovernanceProvider  # noqa: E402
from text import word_set  # noqa: E402


def _runtime_context():
    repo_root = Path(os.environ.get("AGENTIC_PROJECT_ROOT", AGENT_ROOT.parent)).resolve()
    identity = derive_project_identity(repo_root, os.environ.get("AGENTIC_GIT_REMOTE"))
    config_path = Path(os.environ.get(
        "AGENTIC_MEMORY_CONFIG", AGENT_ROOT / "memory/orchestration/config.json"
    ))
    config = load_config(config_path)
    if config.mode not in {"off", "shadow"}:
        raise RuntimeError(
            f"orchestration mode {config.mode!r} is not supported before Phase 6"
        )
    return identity, config


def _provider_session(identity, mode: str):
    return create_memos_provider(
        AGENT_ROOT,
        identity.project_id,
        mode=mode,
        code_root=os.environ.get("AGENTIC_MEMOS_CODE_ROOT"),
        data_root=os.environ.get("AGENTIC_MEMOS_DATA_ROOT"),
    )


def recall_command(intent: str, output_format: str, legacy: bool, top: int) -> str:
    identity, config = _runtime_context()
    provider = GovernanceProvider(AGENT_ROOT, identity.project_id, word_set)
    if config.mode == "shadow":
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
    if config.mode == "shadow":
        with _provider_session(identity, "shadow") as provider:
            behavioral = provider.health()
    return {
        "schema": "agentic.memory.health.v1",
        "mode": config.mode,
        "project_id": identity.project_id,
        "governance": governance_health,
        "behavioral": behavioral,
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
    with _provider_session(identity, "shadow") as provider:
        results = [provider.record(event) for event in events]
        health = provider.health()
    totals = {
        name: sum(result[name] for result in results)
        for name in ("enqueued", "delivered", "ambiguous", "dead")
    }
    return {
        "status": "recorded",
        "event_ids": [event.event_id for event in events],
        **totals,
        "health": health,
    }


def export_command(limit: int, max_bytes: int) -> dict:
    identity, config = _runtime_context()
    if config.mode != "shadow":
        raise RuntimeError("behavioral export requires orchestration shadow mode")
    if not 1 <= limit <= 100:
        raise ValueError("export limit must be between 1 and 100")
    if not 256 <= max_bytes <= 1024 * 1024:
        raise ValueError("export max-bytes must be between 256 and 1048576")
    with _provider_session(identity, "shadow") as provider:
        return provider.export_shadow(limit=limit, max_bytes=max_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Federated memory orchestration")
    sub = parser.add_subparsers(dest="command", required=True)
    recall = sub.add_parser("recall")
    recall.add_argument("--intent", required=True)
    recall.add_argument("--format", choices=("json", "text"), default="text")
    recall.add_argument("--legacy", action="store_true")
    recall.add_argument("--top", type=int, default=3)
    sub.add_parser("health")
    record = sub.add_parser("record", help="validate and deliver an EventEnvelope")
    record.add_argument("--event", default="-", help="JSON file or - for stdin")
    export = sub.add_parser("export-shadow", help="bounded redacted behavioral export")
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--max-bytes", type=int, default=64 * 1024)
    args = parser.parse_args()
    try:
        if args.command == "recall":
            print(recall_command(args.intent, args.format, args.legacy, args.top))
        elif args.command == "health":
            print(json.dumps(health_command(), indent=2, ensure_ascii=False))
        elif args.command == "record":
            print(json.dumps(record_command(args.event), indent=2, ensure_ascii=False))
        elif args.command == "export-shadow":
            print(json.dumps(
                export_command(args.limit, args.max_bytes), indent=2, ensure_ascii=False,
            ))
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
