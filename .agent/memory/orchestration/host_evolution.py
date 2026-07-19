"""Fail-closed host-model boundary for the opt-in Phase 8 evolution pilot.

This module deliberately does not make raw MemOS prompts safe.  Its public
model adapters accept only the small, allowlisted DTO defined below.  In
particular, source, diffs, paths, subprocess output, and credentials have no
representation in that DTO.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .memos_runtime import (
    EvolutionPilotConfig,
    load_evolution_pilot_config,
)


DTO_SCHEMA = "agentic.memory.host-dto.v1"
_PROJECT_ID = re.compile(r"[0-9a-f]{16}\Z")
_REVISION = re.compile(r"[0-9a-f]{40,64}\Z")
_EVIDENCE_ID = re.compile(r"evi_[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OPERATIONS = frozenset({
    "l2.induction", "l3.abstraction", "skill.crystallize",
    "decision.repair", "reward.score", "reflection.summarize",
    "retrieval.filter", "candidate.review",
})
_OUTCOMES = frozenset({"success", "failure", "partial", "unknown"})
_QUOTA_CATEGORIES = {
    "l2.induction": "policy",
    "l3.abstraction": "world_model",
    "skill.crystallize": "skill",
    "decision.repair": "other",
    "reward.score": "other",
    "reflection.summarize": "other",
    "retrieval.filter": "other",
    "candidate.review": "other",
}
_DTO_FIELDS = frozenset({
    "schema", "project_id", "repository_revision", "operation", "summaries",
    "evidence_ids", "digests", "outcome_class", "distinct_episode_ids",
})
_SAFE_ENV = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR"})
_SENSITIVE = (
    re.compile(
        r"(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|"
        r"secret(?:[_-](?:access[_-]?)?key)?|password|authorization)"
        r"(?:[_-][a-z0-9]+)*\s*[:=]"
    ),
    re.compile(r"\b(?:sk|gh[opusr]|github_pat)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----"),
    re.compile(r"(?:^|[\s'\"])(?:/Users/|/home/|[A-Za-z]:\\Users\\)"),
)


class HostEvolutionError(RuntimeError):
    """A sanitized, stable boundary error safe to expose to JSON-RPC."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int


def _fail(code: str) -> HostEvolutionError:
    return HostEvolutionError(code)


def load_pilot_config(
    path: str | Path,
    *,
    expected_project_id: str,
    expected_repo_root: str | Path,
) -> EvolutionPilotConfig:
    """Compatibility wrapper around the single runtime config authority."""
    try:
        return load_evolution_pilot_config(
            path, project_id=expected_project_id, repo_root=expected_repo_root,
        )
    except (OSError, ValueError, PermissionError, RuntimeError) as exc:
        raise _fail("pilot_config_invalid") from exc


def _safe_text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _fail("host_dto_invalid_text")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise _fail("host_dto_control_character")
    if any(pattern.search(value) for pattern in _SENSITIVE):
        raise _fail("host_dto_sensitive_content")
    return value


def validate_sanitized_dto(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy the only object that may cross a host-model boundary."""
    if not isinstance(value, Mapping) or set(value) - _DTO_FIELDS:
        raise _fail("host_dto_invalid_fields")
    required = _DTO_FIELDS - {"distinct_episode_ids"}
    if set(value) < required or value.get("schema") != DTO_SCHEMA:
        raise _fail("host_dto_missing_fields")
    if _PROJECT_ID.fullmatch(value.get("project_id", "")) is None:
        raise _fail("host_dto_invalid_project")
    if _REVISION.fullmatch(value.get("repository_revision", "")) is None:
        raise _fail("host_dto_invalid_revision")
    if value.get("operation") not in _OPERATIONS or value.get("outcome_class") not in _OUTCOMES:
        raise _fail("host_dto_invalid_enum")
    summaries = value.get("summaries")
    if not isinstance(summaries, list) or not 1 <= len(summaries) <= 20:
        raise _fail("host_dto_invalid_summaries")
    clean_summaries = [_safe_text(item, maximum=2000) for item in summaries]
    evidence = value.get("evidence_ids")
    digests = value.get("digests")
    episodes = value.get("distinct_episode_ids", [])
    if (not isinstance(evidence, list) or len(evidence) > 100
            or any(not isinstance(item, str) or _EVIDENCE_ID.fullmatch(item) is None for item in evidence)):
        raise _fail("host_dto_invalid_evidence")
    if (not isinstance(digests, list) or len(digests) > 100
            or any(not isinstance(item, str) or _DIGEST.fullmatch(item) is None for item in digests)):
        raise _fail("host_dto_invalid_digests")
    if (not isinstance(episodes, list) or len(episodes) > 100
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item)
                   for item in episodes)):
        raise _fail("host_dto_invalid_episodes")
    clean = dict(value)
    clean["summaries"] = clean_summaries
    clean["evidence_ids"] = list(evidence)
    clean["digests"] = list(digests)
    clean["distinct_episode_ids"] = list(episodes)
    encoded = json.dumps(clean, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > 65_536:
        raise _fail("host_dto_too_large")
    return clean


def request_digest(dto: Mapping[str, Any]) -> str:
    clean = validate_sanitized_dto(dto)
    payload = json.dumps(clean, separators=(",", ":"), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def quota_category(operation: str) -> str:
    """Map only an exact, pinned host operation to its creation budget."""
    try:
        return _QUOTA_CATEGORIES[operation]
    except (KeyError, TypeError) as exc:
        raise _fail("quota_operation_unknown") from exc


def audit_metadata(dto: Mapping[str, Any], *, provider: str, model: str,
                   duration_ms: int, outcome: str, redaction_count: int = 0) -> dict[str, Any]:
    clean = validate_sanitized_dto(dto)
    compact = json.dumps(clean, separators=(",", ":"), sort_keys=True).encode()
    return {
        "schema": "agentic.memory.host-audit.v1",
        "request_digest": "sha256:" + hashlib.sha256(compact).hexdigest(),
        "dto_schema": DTO_SCHEMA,
        "project_id": clean["project_id"],
        "repository_revision": clean["repository_revision"],
        "operation": clean["operation"],
        "provider": _safe_text(provider, maximum=80),
        "model": _safe_text(model, maximum=80),
        "input_bytes": len(json.dumps(clean, separators=(",", ":")).encode()),
        "duration_ms": max(0, int(duration_ms)),
        "outcome": _safe_text(outcome, maximum=80),
        "redaction_count": max(0, int(redaction_count)),
    }


def build_host_environment(base: Mapping[str, str] | None = None, *, home: str) -> dict[str, str]:
    """Construct, never subtract from, an allowlisted process environment."""
    source = base if base is not None else os.environ
    result = {key: value for key, value in source.items()
              if key in _SAFE_ENV and isinstance(value, str)}
    result["HOME"] = str(home)
    return result


class DailyQuotaStore:
    """Owner-only SQLite reservations with digest-based retry idempotency."""
    def __init__(self, path: str | Path, caps: Mapping[str, int]):
        self.path = Path(path)
        self.caps = dict(caps)
        if not self.caps or any(type(v) is not int or v < 0 for v in self.caps.values()):
            raise _fail("quota_invalid_caps")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise _fail("quota_database_not_owner_regular")
            os.fchmod(descriptor, 0o600)
        except OSError as exc:
            raise _fail("quota_database_unsafe") from exc
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    digest TEXT PRIMARY KEY, category TEXT NOT NULL, day TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','complete')),
                    response TEXT
                );
                CREATE INDEX IF NOT EXISTS requests_day_category
                    ON requests(day, category);
            """)
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def reserve_or_get(self, category: str, digest: str, *, day: str | None = None) -> Any | None:
        if category not in self.caps or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", digest):
            raise _fail("quota_invalid_request")
        day = day or datetime.now(timezone.utc).date().isoformat()
        try:
            if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise _fail("quota_invalid_day") from exc
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state, response FROM requests WHERE digest=?", (digest,)).fetchone()
            if row:
                db.commit()
                if row[0] != "complete":
                    # A retry must not be mistaken for a fresh reservation and
                    # issue a duplicate paid/model request concurrently.
                    raise _fail("quota_request_in_progress")
                return json.loads(row[1])
            count = db.execute("SELECT COUNT(*) FROM requests WHERE day=? AND category=?",
                               (day, category)).fetchone()[0]
            if count >= self.caps[category]:
                db.rollback()
                raise _fail("quota_exhausted")
            db.execute("INSERT INTO requests VALUES (?,?,?,?,NULL)",
                       (digest, category, day, "reserved"))
            db.commit()
        return None

    def complete(self, digest: str, response: Any) -> None:
        encoded = json.dumps(response, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > 65_536:
            raise _fail("quota_cache_response_too_large")
        with self._connect() as db:
            cursor = db.execute("UPDATE requests SET state='complete', response=? "
                                "WHERE digest=? AND state='reserved'", (encoded, digest))
            if cursor.rowcount != 1:
                raise _fail("quota_reservation_missing")


def _terminate_group(process: subprocess.Popen, grace: float = 0.2) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    if process.poll() is None:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    # The leader may have exited after forking a detached worker that remains
    # in our fresh process group. SIGKILL the group even when poll() is done.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait()


def run_bounded_command(
    argv: Sequence[str], *, stdin: bytes, cwd: str | Path, env: Mapping[str, str],
    timeout_seconds: float, max_output_bytes: int = 262_144,
) -> CommandResult:
    """Run a fixed argv with stdin-only payload and bounded aggregate output."""
    if (not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
            or not isinstance(stdin, bytes) or len(stdin) > 65_536):
        raise _fail("command_invalid_input")
    if (not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.01 <= float(timeout_seconds) <= 300
            or type(max_output_bytes) is not int
            or not 1 <= max_output_bytes <= 1024 * 1024
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in env.items())):
        raise _fail("command_invalid_limits")
    working_directory = Path(cwd)
    if working_directory.is_symlink() or not working_directory.is_dir():
        raise _fail("command_invalid_cwd")
    started = time.monotonic()
    with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output_file, \
            tempfile.TemporaryFile() as error_file:
        input_file.write(stdin); input_file.seek(0)
        try:
            process = subprocess.Popen(tuple(argv), stdin=input_file, stdout=output_file,
                                       stderr=error_file, cwd=str(working_directory), env=dict(env),
                                       start_new_session=True, close_fds=True)
        except OSError as exc:
            raise _fail("command_start_failed") from exc
        deadline = started + timeout_seconds
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    _terminate_group(process)
                    raise _fail("command_timeout")
                if os.fstat(output_file.fileno()).st_size + os.fstat(error_file.fileno()).st_size > max_output_bytes:
                    _terminate_group(process)
                    raise _fail("command_output_too_large")
                time.sleep(0.01)
            total = os.fstat(output_file.fileno()).st_size + os.fstat(error_file.fileno()).st_size
            if total > max_output_bytes:
                raise _fail("command_output_too_large")
            output_file.seek(0); error_file.seek(0)
            return CommandResult(tuple(argv), process.returncode, output_file.read(), error_file.read(),
                                 int((time.monotonic() - started) * 1000))
        finally:
            _terminate_group(process)


_CLAUDE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "rationale"],
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reject", "defer"]},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
}


class ClaudeOpusAdapter:
    """True no-tools, ephemeral structured reviewer using existing CLI auth."""
    def __init__(self, *, executable: str = "claude", model: str = "opus",
                 cwd: str | Path, timeout_seconds: float = 60,
                 environment: Mapping[str, str] | None = None,
                 home: str | Path | None = None):
        self.executable = executable
        self.model = model
        self.cwd = Path(cwd)
        self.timeout_seconds = timeout_seconds
        source_environment = environment if environment is not None else os.environ
        auth_home = str(home) if home is not None else source_environment.get("HOME", str(Path.home()))
        self.environment = build_host_environment(source_environment, home=auth_home)

    def complete(self, dto: Mapping[str, Any]) -> dict[str, str]:
        clean = validate_sanitized_dto(dto)
        prompt = json.dumps(clean, separators=(",", ":"), sort_keys=True).encode()
        argv = (
            self.executable, "-p", "--model", self.model, "--safe-mode", "--tools", "",
            "--disable-slash-commands", "--no-session-persistence", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--output-format", "json", "--json-schema",
            json.dumps(_CLAUDE_SCHEMA, separators=(",", ":")),
        )
        result = run_bounded_command(argv, stdin=prompt, cwd=self.cwd, env=self.environment,
                                     timeout_seconds=self.timeout_seconds)
        if result.returncode != 0:
            raise _fail("claude_process_failed")
        try:
            envelope = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _fail("claude_invalid_json") from exc
        if (not isinstance(envelope, dict) or envelope.get("subtype") != "success"
                or envelope.get("is_error") is not False
                or envelope.get("terminal_reason") != "completed"
                or envelope.get("permission_denials") != []
                or type(envelope.get("num_turns")) is not int
                or envelope["num_turns"] < 1
                or not isinstance(envelope.get("duration_api_ms"), (int, float))
                or envelope["duration_api_ms"] <= 0):
            # Covers exit-0 configuration/permission failures which otherwise
            # look successful at the process layer.
            raise _fail("claude_not_executed")
        structured = envelope.get("structured_output")
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except json.JSONDecodeError as exc:
                raise _fail("claude_invalid_result") from exc
        if (not isinstance(structured, dict) or set(structured) != {"decision", "rationale"}
                or structured.get("decision") not in {"approve", "reject", "defer"}):
            raise _fail("claude_invalid_result")
        rationale = _safe_text(structured.get("rationale"), maximum=2000)
        return {"decision": structured["decision"], "rationale": rationale}


class CodexGPTAdapter:
    """Disabled until Codex exposes a preventive no-tools execution mode."""
    def complete(self, dto: Mapping[str, Any]) -> dict[str, Any]:
        validate_sanitized_dto(dto)
        raise _fail("codex_no_tools_unavailable")


__all__ = [
    "ClaudeOpusAdapter", "CodexGPTAdapter", "CommandResult", "DailyQuotaStore",
    "EvolutionPilotConfig", "HostEvolutionError", "audit_metadata",
    "build_host_environment", "load_pilot_config", "request_digest",
    "quota_category", "run_bounded_command", "validate_sanitized_dto",
]
