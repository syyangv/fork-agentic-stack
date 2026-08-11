"""Runs after every action. Appends a structured entry to episodic memory."""
import datetime, os, re
from ._provenance import build_source
from ._episodic_io import append_jsonl

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
EPISODIC = os.path.join(ROOT, "memory/episodic/AGENT_LEARNINGS.jsonl")

_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|password|secret|authorization)\b\s*"
    r"(?:=|:|\s)\s*)([^\s,;]+)|\b(?:sk|ghp|github_pat)-?[A-Za-z0-9_]{12,}\b"
)


def _redact(value) -> str:
    """Remove common credential shapes before durable episodic storage."""
    text = str(value)
    return _SECRET.sub(lambda m: (m.group(1) or "") + "[REDACTED]", text)


def log_execution(skill_name, action, result, success, reflection="",
                  importance=5, confidence=0.5, evidence_ids=None,
                  pain_score=None):
    """Log a structured episodic entry.

    pain_score: override the default (2 for success, 7 for failure). Higher
    values help recurring observations become review candidates. Candidate
    acceptance remains an explicit human decision.
    """
    if pain_score is None:
        pain_score = 2 if success else 7
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "skill": skill_name,
        "action": _redact(action)[:200],
        "result": "success" if success else "failure",
        "detail": _redact(result)[:500],
        "pain_score": pain_score,
        "importance": importance,
        "reflection": _redact(reflection)[:500],
        "confidence": confidence,
        "source": build_source(skill_name),
        "evidence_ids": list(evidence_ids) if evidence_ids else [],
    }
    return append_jsonl(EPISODIC, entry)
