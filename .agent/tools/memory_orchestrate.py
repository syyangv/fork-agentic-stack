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
from orchestration.orchestrator import build_governance_packet, format_packet_text  # noqa: E402
from orchestration.providers.governance import GovernanceProvider  # noqa: E402
from text import word_set  # noqa: E402


def recall_command(intent: str, output_format: str, legacy: bool, top: int) -> str:
    repo_root = Path(os.environ.get("AGENTIC_PROJECT_ROOT", AGENT_ROOT.parent)).resolve()
    identity = derive_project_identity(repo_root, os.environ.get("AGENTIC_GIT_REMOTE"))
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Federated memory orchestration")
    sub = parser.add_subparsers(dest="command", required=True)
    recall = sub.add_parser("recall")
    recall.add_argument("--intent", required=True)
    recall.add_argument("--format", choices=("json", "text"), default="text")
    recall.add_argument("--legacy", action="store_true")
    recall.add_argument("--top", type=int, default=3)
    args = parser.parse_args()
    if args.command == "recall":
        print(recall_command(args.intent, args.format, args.legacy, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
