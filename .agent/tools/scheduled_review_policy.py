"""Deterministic scheduled triage that can reject noise but never accept."""
from __future__ import annotations

import datetime
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


NOISE_PATTERNS = (
    r"Wrote .*(settings\.json|WORKSPACE\.md|REVIEW_QUEUE\.md|AGENT_LEARNINGS\.jsonl)",
    r"Patched .*(WORKSPACE\.md|REVIEW_QUEUE\.md)",
    r"^(Patched|Edited) .+ \(\+\d+/-\d+ lines\)$",
    r"^Wrote (?!.*(LESSONS\.md|DOMAIN_KNOWLEDGE\.md|DECISIONS\.md|permissions\.md)).+ \(\d+ lines\)$",
    r"Tool \w+ completed (successfully|with failure)",
    r"^(Ran|bash): .*\.(json|jsonl|plist)$",
    r"Edited .*(\.claude/projects|\.agent/memory).*/.*: replaced",
    r"High-stakes op completed \((prod|staging|deploy|production)\):",
    r"^Ran: ",
)
_NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)
MAX_SNAPSHOT_CANDIDATES = 10
MAX_TRIAGE_CANDIDATES = 1000
MAX_MAINTENANCE_STATE_BYTES = 64 * 1024
MAX_LOCAL_CONFIG_BYTES = 16 * 1024
MAX_MAINTENANCE_AGE = datetime.timedelta(hours=36)
_LOCAL_CONFIG_KEYS = {
    "schema", "obsidian_path", "notification", "maintenance_schedule",
    "review_schedule", "review_server_host", "review_server_port",
}


@dataclass(frozen=True)
class TriageDecision:
    needs_review: list[dict]
    rejected: list[dict]


def triage_candidates(candidates: list[dict]) -> TriageDecision:
    """Return deterministic junk rejections and everything requiring a human."""
    if not isinstance(candidates, list) or len(candidates) > MAX_TRIAGE_CANDIDATES:
        raise ValueError("scheduled review candidates must be a bounded list")
    needs_review, rejected = [], []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("claim"), str):
            raise ValueError("scheduled review candidate is malformed")
        if _NOISE_RE.search(candidate["claim"]):
            rejected.append(candidate)
        else:
            needs_review.append(candidate)
    return TriageDecision(needs_review=needs_review, rejected=rejected)


def utc_now() -> str:
    """Provide a patchable UTC clock for deterministic scheduled policy tests."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _parse_timestamp(value: object) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def _maintenance_is_current(state_path: Path, now: datetime.datetime) -> bool:
    """Inspect bounded state metadata only; malformed state degrades safely."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(state_path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        chunks: list[bytes] = []
        remaining = MAX_MAINTENANCE_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > MAX_MAINTENANCE_STATE_BYTES:
            return False
        state = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or state.get("last_status") != "success"
    ):
        return False
    last_success = _parse_timestamp(state.get("last_success_at"))
    if last_success is None:
        return False
    age = now - last_success
    return datetime.timedelta(0) <= age <= MAX_MAINTENANCE_AGE


def _notification_outcome(config_path: Path | None) -> str:
    """Return preference-derived intent without exposing local config values."""
    if config_path is None:
        return "disabled"
    descriptor = -1
    try:
        descriptor = os.open(
            config_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return "disabled"
        chunks: list[bytes] = []
        remaining = MAX_LOCAL_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > MAX_LOCAL_CONFIG_BYTES:
            return "disabled"
        value = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "disabled"
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(value, dict)
        or set(value) != _LOCAL_CONFIG_KEYS
        or value.get("schema") != "agentic.memory.scheduled-local.v1"
    ):
        return "disabled"
    return (
        "requested_deferred"
        if value.get("notification") == "requested"
        else "disabled"
    )


def prepare_review_snapshot(
    queue_path: str | Path,
    maintenance_state_path: str | Path,
    local_config_path: str | Path | None = None,
) -> dict:
    """Return bounded, body-free review intent and honest maintenance degradation.

    The queue is deliberately checked only with ``lstat``.  Its candidate
    content remains for the explicit human review surface and is never read or
    rendered by an unattended policy/status command.
    """
    queue = Path(queue_path)
    now = _parse_timestamp(utc_now())
    maintenance_current = (
        now is not None and _maintenance_is_current(Path(maintenance_state_path), now)
    )
    queue_present = _regular_file(queue)
    status = (
        "maintenance_stale_or_failed" if not maintenance_current
        else "review_ready" if queue_present
        else "no_review_queue"
    )
    return {
        "status": status,
        "queue_present": queue_present,
        "authority": "no_auto_accept",
        "snapshot": {
            "intent": "bounded_metadata_only",
            "max_candidates": MAX_SNAPSHOT_CANDIDATES,
        },
        "notification": _notification_outcome(
            Path(local_config_path) if local_config_path is not None else None,
        ),
    }
