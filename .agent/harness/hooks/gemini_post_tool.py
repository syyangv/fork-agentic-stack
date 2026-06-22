#!/usr/bin/env python3
"""Normalize Gemini AfterTool payloads into agentic-stack episodic entries."""
from __future__ import annotations

import json
import os
import re
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

sys.path.insert(0, os.path.join(AGENT_ROOT, "harness"))
sys.path.insert(0, os.path.join(AGENT_ROOT, "tools"))

from hooks.claude_code_post_tool import (  # noqa: E402
    _action_label,
    _detail,
    _importance,
    _is_success,
    _pain_score,
    _reflection,
)
from hooks.on_failure import on_failure  # noqa: E402
from hooks.post_execution import log_execution  # noqa: E402


TOOL_NAME_MAP = {
    "run_shell_command": "Bash",
    "replace": "Edit",
    "write_file": "Write",
    "read_file": "Read",
    "write_todos": "TodoWrite",
    "web_fetch": "WebFetch",
}

_EXIT_CODE_RE = re.compile(r"^\s*Exit Code:\s*(-?\d+)\s*$", re.MULTILINE)
_STDOUT_RE = re.compile(
    r"^\s*Stdout:\s*(.*?)^\s*Stderr:\s*",
    re.MULTILINE | re.DOTALL,
)
_STDERR_RE = re.compile(
    r"^\s*Stderr:\s*(.*?)^\s*Exit Code:\s*",
    re.MULTILINE | re.DOTALL,
)


def _canonical_tool_name(tool_name: str) -> str:
    return TOOL_NAME_MAP.get(tool_name, tool_name)


def _coerce_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _extract_error(resp: dict) -> str:
    error = resp.get("error")
    if isinstance(error, str):
        return error.strip()
    if isinstance(error, dict):
        for key in ("message", "error", "details"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalize_todos(todos: object) -> list[dict]:
    if not isinstance(todos, list):
        return []
    normalized = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        mapped = dict(todo)
        if "content" not in mapped and isinstance(mapped.get("description"), str):
            mapped["content"] = mapped["description"]
        normalized.append(mapped)
    return normalized


def _normalize_tool_input(tool_name: str, tool_input: object) -> dict:
    if not isinstance(tool_input, dict):
        return {"raw": _coerce_text(tool_input)}
    normalized = dict(tool_input)
    if tool_name == "write_todos":
        normalized["todos"] = _normalize_todos(tool_input.get("todos"))
    return normalized


def _parse_shell_display(display: str) -> dict:
    parsed: dict[str, object] = {}
    exit_match = _EXIT_CODE_RE.search(display)
    if exit_match:
        parsed["exit_code"] = int(exit_match.group(1))
    stdout_match = _STDOUT_RE.search(display)
    if stdout_match:
        stdout = stdout_match.group(1).strip()
        if stdout and stdout != "(empty)":
            parsed["stdout"] = stdout
            parsed["output"] = stdout
    stderr_match = _STDERR_RE.search(display)
    if stderr_match:
        stderr = stderr_match.group(1).strip()
        if stderr and stderr != "(empty)":
            parsed["stderr"] = stderr
            parsed.setdefault("error", stderr)
    return parsed


def _normalize_tool_response(tool_name: str, tool_response: object) -> dict:
    if not isinstance(tool_response, dict):
        return {"output": _coerce_text(tool_response)}

    normalized: dict[str, object] = {}
    for key in ("output", "stdout", "result", "text"):
        value = tool_response.get(key)
        if isinstance(value, str) and value:
            normalized["output"] = value
            break

    if "output" not in normalized:
        for key in ("returnDisplay", "llmContent"):
            value = tool_response.get(key)
            text = _coerce_text(value).strip()
            if text:
                normalized["output"] = text
                break

    error = _extract_error(tool_response)
    if error:
        normalized["error"] = error

    if tool_name == "run_shell_command" and isinstance(normalized.get("output"), str):
        parsed = _parse_shell_display(normalized["output"])
        normalized = {**normalized, **parsed}

    return normalized


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        payload = {}

    raw_tool_name = payload.get("tool_name") or "Unknown"
    tool_name = _canonical_tool_name(raw_tool_name)
    tool_input = _normalize_tool_input(raw_tool_name, payload.get("tool_input") or {})
    tool_response = _normalize_tool_response(raw_tool_name, payload.get("tool_response") or {})

    success = _is_success(tool_name, tool_input, tool_response)
    importance = _importance(tool_name, json.dumps(tool_input, ensure_ascii=True))
    action = _action_label(tool_name, tool_input)
    reflection = _reflection(tool_name, tool_input, tool_response, success)
    detail = _detail(tool_name, tool_input, tool_response, success)

    pain_score = _pain_score(importance, success)
    if success:
        log_execution(
            skill_name="gemini",
            action=action,
            result=detail,
            success=True,
            reflection=reflection,
            importance=importance,
            confidence=0.7,
            pain_score=pain_score,
        )
        return

    on_failure(
        skill_name="gemini",
        action=action,
        error=reflection,
        context=detail,
        confidence=0.7,
        importance=importance,
        pain_score=pain_score,
    )


if __name__ == "__main__":
    main()
