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
    "decision.repair", "reward.score", "reflection.score", "reflection.batch",
    "retrieval.filter", "candidate.review",
})
_OUTCOMES = frozenset({"success", "failure", "partial", "unknown"})
_QUOTA_CATEGORIES = {
    "l2.induction": "policy",
    "l3.abstraction": "world_model",
    "skill.crystallize": "skill",
    "decision.repair": "other",
    "reward.score": "other",
    "reflection.score": "other",
    "reflection.batch": "other",
    "retrieval.filter": "other",
    "candidate.review": "other",
}
_DTO_FIELDS = frozenset({
    "schema", "project_id", "repository_revision", "operation", "summaries",
    "evidence_ids", "digests", "outcome_class", "distinct_episode_ids",
})
_SAFE_ENV = frozenset({
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
    "USER", "LOGNAME", "SHELL",
})
_CLAUDE_AUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
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
_PROMPT_SPECS = {
    # Base prompt character length, SHA-256, and exact completeJson schema hint
    # exported/called by the reviewed MemOS 2.0.10 artifact.
    "l2.induction": (
        3382, "7040d4e99346c78d9d17396f93384f41149820c20d50bec13a9dc81af4d6b671",
        '{"title":"...","trigger":"...","procedure":"...","verification":"...","rationale":"...","caveats":["..."],"confidence":0..1,"support_trace_ids":["tr_..."]}',
    ),
    "l3.abstraction": (
        3837, "b2bea0992127a3958c1f57222adbc9061d55b7532c556428f48e42e3d6bbdfca",
        '{"title":"...","domain_tags":["..."],"environment":[{"label":"...","description":"...","evidenceIds":["..."]}],"inference":[...],"constraints":[...],"body":"markdown","confidence":0..1,"supersedes_world_ids":[]}',
    ),
    "skill.crystallize": (
        2648, "9d1fd417e05687bc5e4b0ae4174bb4635a6171ff090d85d22a6e4f3d5deb0f56",
        "skill-crystallize.v2",
    ),
}
_NATIVE_FIELDS = frozenset({"messages", "model", "temperature", "maxTokens", "timeoutMs"})
_LANGUAGE_LINES = frozenset({
    "All natural-language answers MUST be in 简体中文 (zh-CN).",
    "All natural-language answers MUST be in English.",
    "Answer in the same natural language the user used. Do not mix languages.",
})
_JSON_HINT = (
    "Respond with a single valid JSON value and nothing else. Do not wrap in "
    "Markdown code fences. Do not include explanations."
)
_SAFE_NATIVE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_TAXONOMY = frozenset({
    "api", "auth", "backup", "build", "ci", "config", "database", "dependency",
    "docs", "filesystem", "git", "github", "javascript", "network", "node",
    "python", "react", "restore", "security", "shell", "test", "typescript",
})
_SEMANTIC_PATTERNS = {
    "authentication": re.compile(
        r"(?i)(?:\b(?:auth(?:entication|orization)?|credential|login|token)\b|\bapi[_-]?key\b)"
    ),
    "permission": re.compile(r"(?i)\b(?:permission|forbidden|denied|read[- ]?only)\b"),
    "not_found": re.compile(r"(?i)\b(?:not found|missing|no such file|unknown command)\b"),
    "timeout": re.compile(r"(?i)\b(?:timeout|timed out|deadline)\b"),
    "rate_limit": re.compile(r"(?i)\b(?:rate limit|quota|too many requests)\b"),
    "network": re.compile(r"(?i)\b(?:network|dns|connection|http|socket)\b"),
    "dependency": re.compile(r"(?i)\b(?:dependency|package|module|import|install)\b"),
    "test_failure": re.compile(r"(?i)\b(?:assert|test failed|failure|pytest|unittest)\b"),
    "syntax": re.compile(r"(?i)\b(?:syntax|parse|invalid json|malformed)\b"),
    "conflict": re.compile(r"(?i)\b(?:conflict|concurrent|race|lock|in progress)\b"),
    "inspect": re.compile(r"(?i)\b(?:inspect|read|show|search|find|grep|review)\b"),
    "edit": re.compile(r"(?i)\b(?:edit|write|patch|modify|replace|refactor)\b"),
    "execute": re.compile(
        r"(?i)\b(?:run|execute|invoke|command|shell|bash|cat|ls|npm|python|pytest|git)\b"
    ),
    "verify": re.compile(r"(?i)\b(?:verify|validate|check|test|health)\b"),
    "retry": re.compile(r"(?i)\b(?:retry|again|repeat|backoff)\b"),
    "version_control": re.compile(r"(?i)\b(?:git|commit|branch|merge|push|pull request)\b"),
    "source_code": re.compile(r"(?i)(?:\b(?:class|def|function|const|import|return)\b|[{};])"),
    "structured_data": re.compile(r"(?:[\[\]{}]|\b(?:json|yaml|xml|csv)\b)", re.I),
}


class HostEvolutionError(RuntimeError):
    """A sanitized, stable boundary error safe to expose to JSON-RPC."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int


@dataclass(frozen=True, slots=True)
class NativeCompletion:
    text: str
    model: str
    usage: dict[str, int]
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


def build_host_environment(
    base: Mapping[str, str] | None = None, *, home: str,
    include_claude_oauth: bool = False,
) -> dict[str, str]:
    """Construct, never subtract from, an allowlisted process environment."""
    source = base if base is not None else os.environ
    result = {key: value for key, value in source.items()
              if key in _SAFE_ENV and isinstance(value, str)}
    result["HOME"] = str(home)
    if include_claude_oauth and isinstance(source.get(_CLAUDE_AUTH_ENV), str):
        result[_CLAUDE_AUTH_ENV] = source[_CLAUDE_AUTH_ENV]
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
            db.execute("BEGIN IMMEDIATE")
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'"
            ).fetchone()
            if schema and "'failed'" not in schema[0]:
                db.execute("ALTER TABLE requests RENAME TO requests_legacy")
                self._create_schema(db)
                db.execute(
                    "INSERT INTO requests(digest,category,day,state,response,updated_at) "
                    "SELECT digest,category,day,state,response,? FROM requests_legacy",
                    (_utc_now(),),
                )
                db.execute("DROP TABLE requests_legacy")
            else:
                self._create_schema(db)
            db.commit()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                digest TEXT PRIMARY KEY, category TEXT NOT NULL, day TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved','complete','failed')),
                response TEXT, updated_at TEXT NOT NULL
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS requests_day_category ON requests(day,category)"
        )

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
            row = db.execute(
                "SELECT state,response,day FROM requests WHERE digest=?", (digest,)
            ).fetchone()
            if row:
                state, response, reserved_day = row
                if state == "complete":
                    db.commit()
                    return json.loads(response)
                if reserved_day != day:
                    count = db.execute(
                        "SELECT COUNT(*) FROM requests WHERE day=? AND category=?",
                        (day, category),
                    ).fetchone()[0]
                    if count >= self.caps[category]:
                        db.rollback()
                        raise _fail("quota_exhausted")
                    db.execute(
                        "UPDATE requests SET day=?,state='reserved',response=NULL,updated_at=? "
                        "WHERE digest=?", (day, _utc_now(), digest),
                    )
                    db.commit()
                    return None
                db.commit()
                if state == "reserved":
                    # A retry must not be mistaken for a fresh reservation and
                    # issue a duplicate paid/model request concurrently.
                    raise _fail("quota_request_in_progress")
                raise _fail("quota_request_failed")
            count = db.execute("SELECT COUNT(*) FROM requests WHERE day=? AND category=?",
                               (day, category)).fetchone()[0]
            if count >= self.caps[category]:
                db.rollback()
                raise _fail("quota_exhausted")
            db.execute("INSERT INTO requests VALUES (?,?,?,?,NULL,?)",
                       (digest, category, day, "reserved", _utc_now()))
            db.commit()
        return None

    def complete(self, digest: str, response: Any) -> None:
        encoded = json.dumps(response, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > 65_536:
            raise _fail("quota_cache_response_too_large")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE requests SET state='complete',response=?,updated_at=? "
                "WHERE digest=? AND state='reserved'", (encoded, _utc_now(), digest)
            )
            if cursor.rowcount != 1:
                raise _fail("quota_reservation_missing")

    def fail(self, digest: str) -> None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE requests SET state='failed',updated_at=? "
                "WHERE digest=? AND state='reserved'", (_utc_now(), digest),
            )
            if cursor.rowcount != 1:
                raise _fail("quota_reservation_missing")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        self.environment = build_host_environment(
            source_environment, home=auth_home, include_claude_oauth=True,
        )

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


def sanitize_native_memos_request(
    params: Any, *, expected_model: str,
) -> tuple[str, list[dict[str, str]], int]:
    """Classify and sanitize one pinned MemOS host completion request."""
    if not isinstance(params, Mapping) or set(params) - _NATIVE_FIELDS:
        raise _fail("native_request_invalid_fields")
    if params.get("model") not in {None, expected_model}:
        raise _fail("native_request_model_mismatch")
    temperature = params.get("temperature", 0)
    max_tokens = params.get("maxTokens", 1024)
    timeout_ms = params.get("timeoutMs", 45_000)
    if (type(temperature) not in (int, float) or not math.isfinite(float(temperature))
            or not 0 <= float(temperature) <= 2
            or type(max_tokens) is not int or not 1 <= max_tokens <= 65_536
            or type(timeout_ms) is not int or not 100 <= timeout_ms <= 300_000):
        raise _fail("native_request_invalid_limits")
    messages = params.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise _fail("native_request_invalid_messages")
    clean: list[dict[str, str]] = []
    operation: str | None = None
    redactions = 0
    total = 0
    for index, row in enumerate(messages):
        if not isinstance(row, Mapping) or set(row) != {"role", "content"}:
            raise _fail("native_request_invalid_message")
        role = row.get("role")
        content = row.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise _fail("native_request_invalid_message")
        if not content or len(content) > 32_768:
            raise _fail("native_request_invalid_content")
        if index == 0:
            if role != "system":
                raise _fail("native_request_missing_system_prompt")
            operation, sanitized = _classify_native_system(content)
        elif role != "user":
            raise _fail("native_request_invalid_message_order")
        else:
            assert operation is not None
            sanitized, count = _translate_native_payload(operation, content)
            redactions += count
        total += len(sanitized.encode("utf-8"))
        if total > 65_536:
            raise _fail("native_request_too_large")
        clean.append({"role": role, "content": sanitized})
    assert operation is not None
    return operation, clean, redactions


def _classify_native_system(content: str) -> tuple[str, str]:
    for operation, (base_length, fingerprint, schema_hint) in _PROMPT_SPECS.items():
        base = content[:base_length]
        if hashlib.sha256(base.encode("utf-8")).hexdigest() != fingerprint:
            continue
        tail = content[base_length:]
        allowed = {
            f"\n\n{language}\n\n{_JSON_HINT}\n\nExpected shape:\n{schema_hint}"
            for language in _LANGUAGE_LINES
        }
        # Reward/retrieval-style callers are intentionally not enabled by the
        # pilot profile; only the three evolution generators are accepted.
        if tail not in allowed:
            raise _fail("native_request_unknown_system_suffix")
        return operation, content
    raise _fail("native_request_unknown_prompt")


def _translate_native_payload(operation: str, content: str) -> tuple[str, int]:
    translators = {
        "l2.induction": _translate_l2_payload,
        "l3.abstraction": _translate_l3_payload,
        "skill.crystallize": _translate_skill_payload,
    }
    try:
        value = translators[operation](content)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _fail("native_request_payload_shape") from exc
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 32_768:
        raise _fail("native_request_payload_too_large")
    # Every translator reconstructs from allowlisted metadata. Raw user/agent,
    # reflection, source, prompt, command, and tool I/O fields never cross.
    return encoded, max(1, content.count("\n") + 1)


def _safe_native_token(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_NATIVE_TOKEN.fullmatch(value) is None:
        raise _fail("native_request_payload_token")
    return value


def _safe_native_id(value: Any, prefix: str) -> str:
    token = _safe_native_token(value)
    if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{1,120}", token):
        raise _fail("native_request_payload_id")
    return token


def _tokens(value: str, *, limit: int = 20) -> list[str]:
    result = []
    for candidate in re.split(r"\s*,\s*", value.strip()):
        if not candidate or candidate == "-":
            continue
        token = _safe_native_token(candidate).lower()
        if token not in _SAFE_TAXONOMY:
            continue
        if token not in result:
            result.append(token)
    return result[:limit]


def _semantic_classes(*values: str) -> list[str]:
    text = "\n".join(values)
    classes = [name for name, pattern in _SEMANTIC_PATTERNS.items() if pattern.search(text)]
    return classes or ["unspecified"]


def _tool_classes(value: str) -> list[str]:
    names = re.findall(r"(?:^|;\s*)([A-Za-z][A-Za-z0-9_.:-]{0,127})\(", value)
    classes = []
    for name in names:
        lowered = name.lower()
        if any(token in lowered for token in ("test", "pytest", "unittest")):
            category = "test"
        elif lowered in {"bash", "shell", "exec", "terminal"}:
            category = "shell"
        elif "git" in lowered:
            category = "version_control"
        elif any(token in lowered for token in ("read", "write", "edit", "file", "patch")):
            category = "filesystem"
        elif any(token in lowered for token in ("search", "grep", "find", "query")):
            category = "search"
        else:
            category = "other"
        if category not in classes:
            classes.append(category)
    return classes


def _bounded_number(value: Any, *, minimum: float = -1, maximum: float = 1) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise _fail("native_request_payload_number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise _fail("native_request_payload_number")
    return number


def _translate_l2_payload(content: str) -> dict[str, Any]:
    signature = re.search(r"(?m)^PATTERN_SIGNATURE: (.+)$", content)
    blocks = re.split(r"(?m)^---\s*$", content)[1:]
    if signature is None or not 1 <= len(blocks) <= 20:
        raise _fail("native_request_payload_shape")
    traces = []
    for block in blocks:
        trace_id = re.search(r"(?m)^id: (\S+)$", block)
        episode_id = re.search(r"(?m)^episode: (\S+)$", block)
        tags = re.search(r"(?m)^tags: (.*)$", block)
        user = re.search(r"(?m)^user: (.*)$", block)
        agent = re.search(r"(?m)^agent: (.*)$", block)
        tools = re.search(r"(?m)^tools: (.*)$", block)
        reflection = re.search(r"(?m)^reflection: (.*)$", block)
        scores = re.search(r"(?m)^V: (-?\d+(?:\.\d+)?)\s+alpha: (-?\d+(?:\.\d+)?)$", block)
        if None in (trace_id, episode_id, tags, user, agent, tools, reflection, scores):
            raise _fail("native_request_payload_shape")
        value = _bounded_number(float(scores.group(1)))
        traces.append({
            "trace_id": _safe_native_id(trace_id.group(1), "tr_"),
            "episode_id": _safe_native_id(episode_id.group(1), "ep_"),
            "tags": _tokens(tags.group(1)),
            "tool_classes": _tool_classes(tools.group(1)),
            "state_classes": _semantic_classes(user.group(1)),
            "action_classes": _semantic_classes(agent.group(1), tools.group(1)),
            "reflection_classes": _semantic_classes(reflection.group(1)),
            "outcome_class": "success" if value > 0 else "failure" if value < 0 else "unknown",
            "value": value,
            "alpha": _bounded_number(float(scores.group(2)), minimum=0),
        })
    return {
        "schema": "agentic.memory.memos-l2-metadata.v1",
        "pattern_digest": "sha256:" + hashlib.sha256(signature.group(1).encode()).hexdigest(),
        "traces": traces,
    }


def _translate_l3_payload(content: str) -> dict[str, Any]:
    cluster = re.search(r"(?m)^CLUSTER_KEY: (.+)$", content)
    admission = re.search(r"(?m)^ADMISSION: (strict|loose) \(cohesion=(-?\d+(?:\.\d+)?)\)$", content)
    domains = re.search(r"(?m)^DOMAIN_TAGS: (.*)$", content)
    policy_rows = re.findall(
        r"(?ms)^id: (\S+)\n.*?^title: (.*?)\n^trigger: (.*?)\n^procedure: (.*?)\n"
        r"^verification: (.*?)\n^boundary: (.*?)\n^support: (\d+)\s+gain: "
        r"(-?\d+(?:\.\d+)?)\s+status: (\S+)$",
        content,
    )
    if None in (cluster, admission, domains) or not 1 <= len(policy_rows) <= 50:
        raise _fail("native_request_payload_shape")
    policies = [{
        "policy_id": _safe_native_id(policy_id, "po_"),
        "support": int(support),
        "gain": _bounded_number(float(gain)),
        "status": _safe_native_token(status),
        "trigger_classes": _semantic_classes(trigger),
        "procedure_classes": _semantic_classes(procedure),
        "verification_classes": _semantic_classes(verification),
        "boundary_classes": _semantic_classes(boundary),
        "title_digest": "sha256:" + hashlib.sha256(title.encode()).hexdigest(),
    } for (policy_id, title, trigger, procedure, verification, boundary,
           support, gain, status) in policy_rows]
    return {
        "schema": "agentic.memory.memos-l3-metadata.v1",
        "cluster_digest": "sha256:" + hashlib.sha256(cluster.group(1).encode()).hexdigest(),
        "admission": admission.group(1),
        "cohesion": _bounded_number(float(admission.group(2)), minimum=0),
        "domain_tags": _tokens(domains.group(1)),
        "policies": policies,
    }


def _translate_skill_payload(content: str) -> dict[str, Any]:
    payload = json.loads(content)
    if not isinstance(payload, dict) or not isinstance(payload.get("policy"), dict):
        raise _fail("native_request_payload_shape")
    policy = payload["policy"]
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
        raise _fail("native_request_payload_shape")
    clean_evidence = []
    for row in evidence:
        if not isinstance(row, dict):
            raise _fail("native_request_payload_shape")
        clean_evidence.append({
            "trace_id": _safe_native_id(row.get("id"), "tr_"),
            "episode_id": _safe_native_id(row.get("episodeId"), "ep_"),
            "value": _bounded_number(row.get("value")),
            "alpha": None if row.get("alpha") is None else _bounded_number(row.get("alpha"), minimum=0),
            "tags": _tokens(",".join(row.get("tags", [])[:20])),
        })
    tools = payload.get("evidence_tools", [])
    if not isinstance(tools, list) or len(tools) > 50:
        raise _fail("native_request_payload_shape")
    return {
        "schema": "agentic.memory.memos-skill-metadata.v1",
        "policy": {
            "policy_id": _safe_native_id(policy.get("id"), "po_"),
            "support": int(_bounded_number(policy.get("support"), minimum=0, maximum=1_000_000)),
            "gain": _bounded_number(policy.get("gain")),
            "trigger_classes": _semantic_classes(str(policy.get("trigger", ""))),
            "procedure_classes": _semantic_classes(str(policy.get("procedure", ""))),
            "verification_classes": _semantic_classes(str(policy.get("verification", ""))),
            "boundary_classes": _semantic_classes(str(policy.get("boundary", ""))),
        },
        "evidence": clean_evidence,
        "evidence_tool_classes": _tool_classes(
            "; ".join(f"{_safe_native_token(item)}()" for item in tools)
        ),
    }


class ClaudeOpusNativeAdapter(ClaudeOpusAdapter):
    """No-tools Claude adapter for pinned, sanitized MemOS conversations."""

    def complete_messages(self, messages: Sequence[Mapping[str, str]]) -> NativeCompletion:
        payload = json.dumps({
            "instruction": "Complete the supplied conversation. Return only the assistant response requested by its system message, with no markdown fence.",
            "messages": list(messages),
        }, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        argv = (
            self.executable, "-p", "--model", self.model, "--safe-mode", "--tools", "",
            "--disable-slash-commands", "--no-session-persistence", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--output-format", "json",
        )
        result = run_bounded_command(
            argv, stdin=payload, cwd=self.cwd, env=self.environment,
            timeout_seconds=self.timeout_seconds,
        )
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
                or type(envelope.get("num_turns")) is not int or envelope["num_turns"] < 1
                or not isinstance(envelope.get("duration_api_ms"), (int, float))
                or envelope["duration_api_ms"] <= 0
                or not isinstance(envelope.get("result"), str)):
            raise _fail("claude_not_executed")
        text = envelope["result"].strip()
        if not text or len(text.encode("utf-8")) > 65_536:
            raise _fail("claude_invalid_result")
        if any(pattern.search(text) for pattern in _SENSITIVE[:4]):
            raise _fail("claude_sensitive_result")
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        prompt_tokens = _bounded_token_count(usage.get("input_tokens"))
        completion_tokens = _bounded_token_count(usage.get("output_tokens"))
        return NativeCompletion(
            text=text, model=self.model,
            usage={
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "totalTokens": prompt_tokens + completion_tokens,
            },
            duration_ms=max(0, int(envelope.get("duration_api_ms", result.duration_ms))),
        )


def _bounded_token_count(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= 100_000_000 else 0


class MemosOpusHostHandler:
    """Quota-controlled native reverse handler constructed by the factory."""

    def __init__(
        self, *, adapter: ClaudeOpusNativeAdapter, quota: DailyQuotaStore,
        audit_file: str | Path, expected_model: str, project_id: str,
        repository_revision: str,
    ) -> None:
        if (_PROJECT_ID.fullmatch(project_id) is None
                or _REVISION.fullmatch(repository_revision) is None):
            raise _fail("native_handler_identity_invalid")
        self.adapter = adapter
        self.quota = quota
        self.audit_file = Path(audit_file)
        self.expected_model = expected_model
        self.project_id = project_id
        self.repository_revision = repository_revision

    def __call__(self, params: Any) -> dict[str, Any]:
        try:
            operation, messages, redactions = sanitize_native_memos_request(
                params, expected_model=self.expected_model,
            )
        except BaseException as exc:
            self._audit_failure(
                digest=_opaque_native_digest(params), operation="unclassified", exc=exc,
            )
            raise
        digest_payload = {
            "schema": "agentic.memory.host-cache.v2",
            "project_id": self.project_id,
            "repository_revision": self.repository_revision,
            "provider": "claude", "model": self.expected_model,
            "operation": operation, "messages": messages,
            "temperature": params.get("temperature", 0),
            "max_tokens": params.get("maxTokens", 1024),
            "timeout_ms": params.get("timeoutMs", 45_000),
        }
        digest = "sha256:" + hashlib.sha256(json.dumps(
            digest_payload, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
        try:
            cached = self.quota.reserve_or_get(quota_category(operation), digest)
        except BaseException as exc:
            self._audit_failure(digest=digest, operation=operation, exc=exc)
            raise
        if cached is not None:
            output_bytes = (
                len(str(cached.get("text", "")).encode("utf-8"))
                if isinstance(cached, dict) else 0
            )
            usage = cached.get("usage", {}) if isinstance(cached, dict) else {}
            self._audit({
                "schema": "agentic.memory.host-audit.v1",
                "request_digest": digest, "operation": operation,
                "provider": "claude", "model": self.expected_model,
                "duration_ms": 0, "outcome": "cached",
                "redaction_count": redactions,
                "project_id": self.project_id,
                "repository_revision": self.repository_revision,
                "input_bytes": _native_message_bytes(messages),
                "output_bytes": output_bytes,
                "prompt_tokens": _bounded_token_count(usage.get("promptTokens")),
                "completion_tokens": _bounded_token_count(usage.get("completionTokens")),
            })
            return cached
        started = time.monotonic()
        try:
            completion = self.adapter.complete_messages(messages)
            response = {
                "text": completion.text, "model": completion.model,
                "usage": completion.usage, "durationMs": completion.duration_ms,
            }
            self.quota.complete(digest, response)
            outcome = "ok"
            return response
        except BaseException as exc:
            outcome = "failed"
            failure_class = _failure_class(exc)
            self.quota.fail(digest)
            raise
        finally:
            row = {
                "schema": "agentic.memory.host-audit.v1",
                "request_digest": digest, "operation": operation,
                "provider": "claude", "model": self.expected_model,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "outcome": outcome, "redaction_count": redactions,
                "project_id": self.project_id,
                "repository_revision": self.repository_revision,
                "input_bytes": _native_message_bytes(messages),
            }
            if outcome == "failed":
                row["failure_class"] = failure_class
                row["output_bytes"] = 0
            else:
                row["output_bytes"] = len(completion.text.encode("utf-8"))
                row["prompt_tokens"] = completion.usage["promptTokens"]
                row["completion_tokens"] = completion.usage["completionTokens"]
            self._audit(row)

    def _audit_failure(self, *, digest: str, operation: str, exc: BaseException) -> None:
        self._audit({
            "schema": "agentic.memory.host-audit.v1",
            "request_digest": digest, "operation": operation,
            "provider": "claude", "model": self.expected_model,
            "duration_ms": 0, "outcome": "rejected", "redaction_count": 0,
            "failure_class": _failure_class(exc),
            "project_id": self.project_id,
            "repository_revision": self.repository_revision,
            "input_bytes": 0, "output_bytes": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
        })

    def _audit(self, row: Mapping[str, Any]) -> None:
        self.audit_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.audit_file.parent, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.audit_file, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, json.dumps(row, separators=(",", ":")).encode() + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _failure_class(exc: BaseException) -> str:
    if isinstance(exc, HostEvolutionError) and re.fullmatch(r"[a-z0-9_]{1,80}", str(exc)):
        return str(exc)
    return "internal_failure"


def _opaque_native_digest(params: Any) -> str:
    try:
        encoded = json.dumps(
            params, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        encoded = type(params).__name__.encode("ascii", errors="replace")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _native_message_bytes(messages: Sequence[Mapping[str, str]]) -> int:
    return len(json.dumps(
        messages, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8"))


class CodexGPTAdapter:
    """Disabled until Codex exposes a preventive no-tools execution mode."""
    def complete(self, dto: Mapping[str, Any]) -> dict[str, Any]:
        validate_sanitized_dto(dto)
        raise _fail("codex_no_tools_unavailable")


__all__ = [
    "ClaudeOpusAdapter", "ClaudeOpusNativeAdapter", "CodexGPTAdapter", "CommandResult",
    "MemosOpusHostHandler", "NativeCompletion", "DailyQuotaStore",
    "EvolutionPilotConfig", "HostEvolutionError", "audit_metadata",
    "build_host_environment", "load_pilot_config", "request_digest",
    "quota_category", "run_bounded_command", "sanitize_native_memos_request",
    "validate_sanitized_dto",
]
