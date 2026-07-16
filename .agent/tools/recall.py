"""Proactive, auditable lesson recall for the current intent."""
import argparse
import json
import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BASE, "harness"))
sys.path.insert(0, os.path.join(BASE, "memory"))
from text import word_set  # noqa: E402
from orchestration import governance_recall as recall_core  # noqa: E402

LESSONS_JSONL = os.path.join(BASE, "memory/semantic/lessons.jsonl")
LESSONS_MD = os.path.join(BASE, "memory/semantic/LESSONS.md")


def _load_structured():
    return recall_core.load_structured(LESSONS_JSONL)


def _load_markdown_fallback():
    return recall_core.load_markdown(LESSONS_MD)


def _score(claim, conditions, query_words):
    return recall_core.lexical_score(claim, conditions, query_words, word_set)


def _merge_sources():
    return recall_core.merge_sources(LESSONS_JSONL, LESSONS_MD)


def recall(intent, top_k=3, min_score=0.01):
    return recall_core.recall_lessons(
        intent, LESSONS_JSONL, LESSONS_MD, word_set, top_k, min_score
    )


def log_recall(intent, result, meta):
    try:
        sys.path.insert(0, os.path.join(BASE, "tools"))
        from memory_reflect import reflect  # noqa: E402
        detail = {
            "returned": [item["claim"][:80] for item in result],
            "considered": meta["considered"],
            "source_counts": meta.get("source_counts", {}),
            "only_md_available": meta.get("only_md_available", False),
        }
        reflect("proactive-recall", f"recall:{intent[:80]}",
                json.dumps(detail, ensure_ascii=False), success=True, importance=6)
    except Exception as exc:
        print(f"(warning: recall log failed: {exc})", file=sys.stderr)


def format_pretty(intent, result, meta):
    return recall_core.format_pretty(intent, result, meta)


def main():
    parser = argparse.ArgumentParser(description="Surface relevant lessons for an intent.")
    parser.add_argument("intent", help="Free-text description of what you're about to do.")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of pretty.")
    parser.add_argument("--quiet", action="store_true", help="Don't log to episodic.")
    args = parser.parse_args()
    result, meta = recall(args.intent, top_k=args.top)
    if not args.quiet:
        log_recall(args.intent, result, meta)
    if args.json:
        print(json.dumps({"result": result, "meta": meta}, indent=2))
    else:
        print(format_pretty(args.intent, result, meta))


if __name__ == "__main__":
    main()
