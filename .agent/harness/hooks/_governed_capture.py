"""Shared governed episodic capture for harness-specific event adapters.

Adapters normalize their native payloads before calling :func:`record_tool_event`.
This module only appends observations.  It cannot accept candidates, graduate
lessons, or modify governed semantic memory.
"""
from __future__ import annotations

import json

from .claude_code_post_tool import (
    _action_label,
    _detail,
    _importance,
    _is_success,
    _pain_score,
    _reflection,
)
from .on_failure import on_failure
from .post_execution import log_execution


def record_tool_event(harness: str, tool_name: str, tool_input: dict,
                      tool_response: dict) -> None:
    """Append one normalized tool observation to governed episodic memory."""
    success = _is_success(tool_name, tool_input, tool_response)
    importance = _importance(tool_name, json.dumps(tool_input, ensure_ascii=True))
    action = _action_label(tool_name, tool_input)
    reflection = _reflection(tool_name, tool_input, tool_response, success)
    detail = _detail(tool_name, tool_input, tool_response, success)
    pain_score = _pain_score(importance, success)
    if success:
        log_execution(
            skill_name=harness, action=action, result=detail, success=True,
            reflection=reflection, importance=importance, confidence=0.7,
            pain_score=pain_score,
        )
    else:
        on_failure(
            skill_name=harness, action=action, error=reflection,
            context=detail, confidence=0.7, importance=importance,
            pain_score=pain_score,
        )
