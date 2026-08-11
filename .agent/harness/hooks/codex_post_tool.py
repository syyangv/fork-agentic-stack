#!/usr/bin/env python3
"""Normalize Codex shell PostToolUse payloads into governed episodic records.

Codex currently emits Pre/Post tool hooks for shell commands only.  This
adapter deliberately claims only that coverage; non-shell work is captured by
explicit reflection when significant.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(AGENT_ROOT, "harness"))

from hooks._governed_capture import record_tool_event  # noqa: E402


def normalize(payload: object) -> tuple[str, dict, dict]:
    """Accept current Codex shell-hook fields and conservative aliases."""
    if not isinstance(payload, dict):
        payload = {}
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "Bash")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        command = payload.get("command")
        tool_input = {"command": command} if isinstance(command, str) else {}
    response = payload.get("tool_response") or payload.get("response")
    if not isinstance(response, dict):
        response = {}
    for key in ("output", "stdout", "stderr", "error", "exit_code", "interrupted"):
        if key in payload and key not in response:
            response[key] = payload[key]
    return tool_name, tool_input, response


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        payload = {}
    tool_name, tool_input, response = normalize(payload)
    record_tool_event("codex", tool_name, tool_input, response)
    # Hook success is intentionally silent.  In particular, Stop-compatible
    # commands must never emit human-readable stdout.


if __name__ == "__main__":
    main()
