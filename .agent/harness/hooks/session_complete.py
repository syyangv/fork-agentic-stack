#!/usr/bin/env python3
"""Append a bounded, metadata-only harness session-completion observation."""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(AGENT_ROOT, "harness"))

from hooks.post_execution import log_execution  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("harness", choices=("codex", "claude-code", "gemini"))
    args = parser.parse_args()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        payload = {}
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    detail = "session completion observed"
    if isinstance(session_id, str) and session_id:
        detail += f"; session_id={session_id[:80]}"
    log_execution(
        skill_name=args.harness,
        action="session-complete",
        result=detail,
        success=True,
        reflection="Session completion captured for governed review staging.",
        importance=3,
        confidence=1.0,
        pain_score=1,
    )
    # Emit nothing: valid for Claude/Gemini and avoids invalid Codex Stop JSON.


if __name__ == "__main__":
    main()
