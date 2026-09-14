"""Structured result capture for the P5 knowledge-draft boundary.

Only an explicitly marked JSON candidate block is considered.  The rest of a
model answer is deliberately ignored, never stored, and never used to repair
an invalid candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import tempfile
import unicodedata
from typing import Any, Iterable, Mapping

from .config import VaultRegistration
from .drafts import DraftStore
from .scope import validate_vault_path


CANDIDATE_BLOCK_START = "<knowledge-candidate>"
CANDIDATE_BLOCK_END = "</knowledge-candidate>"
FENCED_CANDIDATE_LANGUAGE = "knowledge-candidate"
COMMENT_CANDIDATE_START = "<!-- KNOWLEDGE_CANDIDATE_START -->"
COMMENT_CANDIDATE_END = "<!-- KNOWLEDGE_CANDIDATE_END -->"

MAX_RESULT_CHARS = 200_000
MAX_CANDIDATE_BLOCK_CHARS = 80_000
MAX_SUMMARY_CHARS = 10_000
MAX_TITLE_CHARS = 300
MAX_NOTE_COUNT = 32
MAX_NOTE_CHARS = 12_000
MAX_SOURCE_REF_COUNT = 32
MAX_SOURCE_ID_CHARS = 256
MAX_NOTE_ID_CHARS = 256
MAX_PATH_CHARS = 2_000
MAX_HEADING_CHARS = 500
MAX_EVIDENCE_COUNT = 32
MAX_EVIDENCE_CHARS = 4_000
MAX_UNCERTAINTY_COUNT = 32
MAX_UNCERTAINTY_CHARS = 2_000
MAX_TARGET_PATH_CHARS = 512

_HEX_64 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_BLOCK_PATTERNS = (
    re.compile(
        rf"{re.escape(CANDIDATE_BLOCK_START)}(?P<body>.*?){re.escape(CANDIDATE_BLOCK_END)}",
        re.DOTALL,
    ),
    re.compile(
        rf"^[ \t]*```{re.escape(FENCED_CANDIDATE_LANGUAGE)}[ \t]*\n(?P<body>.*?)^[ \t]*```[ \t]*$",
        re.MULTILINE | re.DOTALL,
    ),
    re.compile(
        rf"{re.escape(COMMENT_CANDIDATE_START)}(?P<body>.*?){re.escape(COMMENT_CANDIDATE_END)}",
        re.DOTALL,
    ),
)


@dataclass(frozen=True)
class CaptureDiagnostic:
    code: str
    field: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.field is not None:
            result["field"] = self.field
        return result


@dataclass(frozen=True)
class CandidateParse:
    candidate: dict[str, Any] | None
    candidate_hash: str | None
    diagnostics: tuple[CaptureDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return self.candidate is not None and not self.diagnostics

    @property
    def status(self) -> str:
        return "ok" if self.ok else "capture_incomplete"


@dataclass(frozen=True)
class CaptureResult:
    capture_status: str
    draft: dict[str, Any] | None
    candidate: dict[str, Any] | None
    candidate_hash: str | None
    diagnostics: tuple[CaptureDiagnostic, ...] = ()

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_status": self.capture_status,
            "success": self.capture_status in {"draft_created", "duplicate"},
            "draft": self.draft,
            "candidate_hash": self.candidate_hash,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def parse_candidate_block(
    result_text: str,
    *,
    source_manifest: Iterable[Mapping[str, Any]] | None = None,
) -> CandidateParse:
    """Extract and validate one marked candidate block from an answer.

    No unmarked JSON or natural-language text is parsed.  ``source_manifest``
    is optional for parser-only use, but capture callers should always provide
    it so source refs can be checked against the task's actual context.
    """

    if not isinstance(result_text, str):
        return CandidateParse(None, None, (_diagnostic("result_not_text", message="Result output must be text."),))
    if len(result_text) > MAX_RESULT_CHARS:
        return CandidateParse(None, None, (_diagnostic("result_oversize", message="Result output exceeds the capture limit."),))

    matches: list[str] = []
    for pattern in _BLOCK_PATTERNS:
        matches.extend(match.group("body") for match in pattern.finditer(result_text))
    if not matches:
        return CandidateParse(None, None, (_diagnostic("candidate_block_missing", message="No delimited knowledge candidate was found."),))
    if len(matches) != 1:
        return CandidateParse(None, None, (_diagnostic("candidate_block_ambiguous", message="Exactly one delimited knowledge candidate is required."),))

    body = matches[0].strip()
    if len(body) > MAX_CANDIDATE_BLOCK_CHARS:
        return CandidateParse(None, None, (_diagnostic("candidate_oversize", message="Candidate block exceeds the capture limit."),))
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return CandidateParse(None, None, (_diagnostic("candidate_json_invalid", message="Candidate block is not valid JSON."),))
    return validate_candidate_payload(payload, source_manifest=source_manifest)


def validate_candidate_payload(
    payload: Any,
    *,
    source_manifest: Iterable[Mapping[str, Any]] | None = None,
) -> CandidateParse:
    """Validate a decoded candidate without applying any model-generated fixes."""

    diagnostics: list[CaptureDiagnostic] = []
    if not isinstance(payload, Mapping):
        return CandidateParse(None, None, (_diagnostic("candidate_schema_invalid", message="Candidate must be a JSON object."),))

    allowed = {
        "summary",
        "title",
        "candidate_notes",
        "source_refs",
        "validation_evidence",
        "uncertainties",
        "target_action",
        "target_path",
        "target",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        diagnostics.append(_diagnostic("candidate_schema_invalid", field=str(unknown[0]), message="Candidate contains an unsupported field."))

    required = ("summary", "candidate_notes", "source_refs", "validation_evidence", "uncertainties")
    for field in required:
        if field not in payload:
            diagnostics.append(_diagnostic("candidate_schema_invalid", field=field, message="Candidate field is required."))

    summary = _bounded_string(payload.get("summary"), "summary", MAX_SUMMARY_CHARS, diagnostics)
    title = _bounded_string(payload.get("title"), "title", MAX_TITLE_CHARS, diagnostics, required=False)
    notes = _normalise_notes(payload.get("candidate_notes"), diagnostics)
    source_refs = _normalise_source_refs(payload.get("source_refs"), "source_refs", diagnostics)
    evidence = _normalise_evidence(payload.get("validation_evidence"), diagnostics)
    uncertainties = _normalise_strings(
        payload.get("uncertainties"),
        "uncertainties",
        MAX_UNCERTAINTY_COUNT,
        MAX_UNCERTAINTY_CHARS,
        diagnostics,
    )

    target_action, target_path = _normalise_target(payload, diagnostics)
    if source_manifest is not None:
        manifest = _normalise_source_refs(source_manifest, "source_manifest", diagnostics)
    else:
        manifest = []
    if source_manifest is not None and source_refs and manifest:
        for source in source_refs:
            if not any(_source_ref_is_within(source, registered) for registered in manifest):
                diagnostics.append(
                    _diagnostic(
                        "source_ref_unrecognized",
                        field="source_refs",
                        message="Candidate source reference is not in the task source manifest.",
                    )
                )
                break
    elif source_manifest is not None and not manifest:
        diagnostics.append(_diagnostic("source_manifest_invalid", field="source_manifest", message="Task source manifest is required and must be valid."))

    if diagnostics:
        return CandidateParse(None, None, tuple(_deduplicate_diagnostics(diagnostics)))
    if summary is None or not notes or not source_refs or evidence is None or uncertainties is None:
        # The explicit diagnostics above normally cover this branch.  Keep it
        # as a fail-closed guard if validation changes in the future.
        return CandidateParse(None, None, (_diagnostic("candidate_schema_invalid", message="Candidate has invalid required data."),))

    normalised: dict[str, Any] = {
        "summary": summary,
        "title": title or _title_from_summary(summary),
        "candidate_notes": notes,
        "source_refs": source_refs,
        "validation_evidence": evidence,
        "uncertainties": uncertainties,
        "target_action": target_action,
        "target_path": target_path,
    }
    provisional_hash = _stable_hash(normalised)
    if target_path is None:
        normalised["target_path"] = _default_target_path(normalised["title"], provisional_hash)
    candidate_hash = _stable_hash(normalised)
    return CandidateParse(normalised, candidate_hash)


def capture_result(
    task_id: str,
    turn_id: str,
    result_text: str,
    *,
    task_status: str,
    turn_status: str | None = None,
    source_manifest: Iterable[Mapping[str, Any]] = (),
    personal_context_used: bool = False,
    persistence_decision: Mapping[str, Any] | None = None,
    draft_store: DraftStore,
    draft_vault: VaultRegistration | None = None,
    work_vault: VaultRegistration | None = None,
) -> CaptureResult:
    """Capture a completed task result and idempotently store one draft.

    The caller supplies any persistence decision from a separate human-owned
    workflow.  This function never creates, infers, or approves that decision.
    """

    if draft_vault is not None and work_vault is not None and draft_vault.path != work_vault.path:
        raise ValueError("Provide only one draft vault destination")
    draft_vault = draft_vault or work_vault

    diagnostics: list[CaptureDiagnostic] = []
    try:
        manifest_input = list(source_manifest)
    except TypeError:
        manifest_input = []
        diagnostics.append(_diagnostic("source_manifest_invalid", field="source_manifest", message="Task source manifest must be a list."))
    if not _valid_identity(task_id):
        diagnostics.append(_diagnostic("task_id_invalid", field="task_id", message="Task ID is required."))
    if not _valid_identity(turn_id):
        diagnostics.append(_diagnostic("turn_id_invalid", field="turn_id", message="Turn ID is required."))
    if diagnostics:
        return _incomplete(draft_store, task_id, turn_id, diagnostics, personal_context_used, ())

    if task_status != "completed" or turn_status != "completed":
        diagnostics.append(_diagnostic("turn_not_completed", message="Only a completed task turn can create a knowledge draft."))
        return _incomplete(
            draft_store,
            task_id,
            turn_id,
            diagnostics,
            personal_context_used,
            _capture_provenance(manifest_input),
        )

    manifest = _safe_provenance(manifest_input)
    parsed = parse_candidate_block(result_text, source_manifest=manifest_input)
    if not parsed.ok:
        return _incomplete(
            draft_store,
            task_id,
            turn_id,
            list(parsed.diagnostics),
            personal_context_used,
            _capture_provenance(manifest),
            candidate_hash=parsed.candidate_hash,
        )
    assert parsed.candidate is not None
    assert parsed.candidate_hash is not None
    candidate = parsed.candidate
    has_personal_context = personal_context_used or any(
        source["vault_id"] == "personal" for source in candidate["source_refs"]
    )

    if has_personal_context and not _approved_persistence_decision(persistence_decision):
        diagnostics.append(
            _diagnostic(
                "personal_quarantined",
                message="Personal-context candidates remain review-only without a separate approved persistence decision.",
            )
        )
        draft_store.record_capture(
            task_id=task_id,
            turn_id=turn_id,
            candidate_hash=parsed.candidate_hash,
            status="quarantined",
            diagnostics=[item.to_dict() for item in diagnostics],
            provenance=_capture_provenance(candidate["source_refs"]),
            personal_context_used=True,
        )
        return CaptureResult("quarantined", None, candidate, parsed.candidate_hash, tuple(diagnostics))

    draft = _draft_from_candidate(task_id, turn_id, parsed.candidate_hash, candidate)
    stored, created = draft_store.put_draft(draft)
    if draft_vault is not None and not has_personal_context:
        _persist_inbox_draft(stored, draft_vault)
    capture_status = "draft_created" if created else "duplicate"
    draft_store.record_capture(
        task_id=task_id,
        turn_id=turn_id,
        candidate_hash=parsed.candidate_hash,
        status=capture_status,
        diagnostics=[],
        provenance=_capture_provenance(candidate["source_refs"]),
        personal_context_used=has_personal_context,
    )
    return CaptureResult(capture_status, stored, candidate, parsed.candidate_hash)


def _draft_from_candidate(
    task_id: str,
    turn_id: str,
    candidate_hash: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    content = _render_draft_content(candidate)
    draft_key = "\0".join((task_id, turn_id, candidate_hash))
    draft_id = "draft_" + hashlib.sha256(draft_key.encode("utf-8")).hexdigest()
    timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "draft_id": draft_id,
        "task_id": task_id,
        "turn_id": turn_id,
        "candidate_hash": candidate_hash,
        "target_path": candidate["target_path"],
        "target_action": candidate["target_action"],
        "draft_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "title": candidate["title"],
        "content": content,
        # Provenance is a separate field from generated Markdown content.
        "source_refs": candidate["source_refs"],
        "validation_level": _validation_level(candidate["validation_evidence"]),
        "validation_evidence": candidate["validation_evidence"],
        "uncertainties": candidate["uncertainties"],
        "status": "draft",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _render_draft_content(candidate: Mapping[str, Any]) -> str:
    lines = [f"# {candidate['title']}", "", str(candidate["summary"]), "", "## Candidate notes", ""]
    for note in candidate["candidate_notes"]:
        if isinstance(note, Mapping):
            note_title = note.get("title")
            if note_title:
                lines.extend([f"### {note_title}", ""])
            lines.extend([str(note["content"]), ""])
        else:
            lines.extend([f"- {note}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _normalise_notes(value: Any, diagnostics: list[CaptureDiagnostic]) -> list[str | dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > MAX_NOTE_COUNT:
        diagnostics.append(_diagnostic("candidate_schema_invalid", field="candidate_notes", message="Candidate notes must be a non-empty bounded list."))
        return []
    notes: list[str | dict[str, str]] = []
    for index, note in enumerate(value):
        field = f"candidate_notes[{index}]"
        if isinstance(note, str):
            text = _bounded_string(note, field, MAX_NOTE_CHARS, diagnostics)
            if text is not None:
                notes.append(text)
            continue
        if not isinstance(note, Mapping):
            diagnostics.append(_diagnostic("candidate_schema_invalid", field=field, message="Candidate note must be text or an object."))
            continue
        if set(note) - {"title", "content"}:
            diagnostics.append(_diagnostic("candidate_schema_invalid", field=field, message="Candidate note contains an unsupported field."))
        note_title = _bounded_string(note.get("title"), f"{field}.title", MAX_TITLE_CHARS, diagnostics, required=False)
        note_content = _bounded_string(note.get("content"), f"{field}.content", MAX_NOTE_CHARS, diagnostics)
        if note_content is not None:
            item: dict[str, str] = {"content": note_content}
            if note_title is not None:
                item["title"] = note_title
            notes.append(item)
    return notes


def _normalise_evidence(value: Any, diagnostics: list[CaptureDiagnostic]) -> list[dict[str, str]] | None:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or len(value) > MAX_EVIDENCE_COUNT:
        diagnostics.append(_diagnostic("candidate_schema_invalid", field="validation_evidence", message="Validation evidence must be a non-empty bounded list or string."))
        return None
    evidence: list[dict[str, str]] = []
    for index, item in enumerate(value):
        field = f"validation_evidence[{index}]"
        if isinstance(item, str):
            text = _bounded_string(item, field, MAX_EVIDENCE_CHARS, diagnostics)
            if text is not None:
                evidence.append({"kind": "agent_reported", "evidence": text})
            continue
        if not isinstance(item, Mapping) or set(item) != {"kind", "evidence"}:
            diagnostics.append(_diagnostic("candidate_schema_invalid", field=field, message="Evidence must identify its provenance kind."))
            continue
        kind = item.get("kind")
        if kind not in {"agent_reported", "externally_observed"}:
            diagnostics.append(_diagnostic("candidate_schema_invalid", field=f"{field}.kind", message="Evidence kind is not supported."))
        text = _bounded_string(item.get("evidence"), f"{field}.evidence", MAX_EVIDENCE_CHARS, diagnostics)
        if kind in {"agent_reported", "externally_observed"} and text is not None:
            evidence.append({"kind": kind, "evidence": text})
    return evidence


def _normalise_source_refs(value: Any, field: str, diagnostics: list[CaptureDiagnostic]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_SOURCE_REF_COUNT:
        diagnostics.append(_diagnostic("source_ref_invalid", field=field, message="Source refs must be a non-empty bounded list."))
        return []
    refs: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        ref = _normalise_source_ref(raw, f"{field}[{index}]", diagnostics)
        if ref is not None:
            refs.append(ref)
    return refs


def _normalise_source_ref(value: Any, field: str, diagnostics: list[CaptureDiagnostic]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        diagnostics.append(_diagnostic("source_ref_invalid", field=field, message="Source ref must be an object."))
        return None
    required = {"source_id", "vault_id", "note_id", "path", "line_start", "line_end"}
    allowed = required | {"heading", "content_hash", "hash"}
    if not required.issubset(value) or set(value) - allowed:
        diagnostics.append(_diagnostic("source_ref_invalid", field=field, message="Source ref has an invalid shape."))
        return None
    source_id = value.get("source_id")
    vault_id = value.get("vault_id")
    note_id = value.get("note_id")
    path = value.get("path")
    heading = value.get("heading")
    line_start = value.get("line_start")
    line_end = value.get("line_end")
    content_hash = value.get("content_hash") or value.get("hash")
    hash_alias = value.get("hash")
    valid = (
        isinstance(source_id, str) and bool(_ID.fullmatch(source_id)) and len(source_id) <= MAX_SOURCE_ID_CHARS
        and vault_id in {"work", "personal"}
        and isinstance(note_id, str) and 0 < len(note_id) <= MAX_NOTE_ID_CHARS
        and isinstance(path, str) and 0 < len(path) <= MAX_PATH_CHARS and _safe_relative_path(path)
        and (heading is None or isinstance(heading, str) and len(heading) <= MAX_HEADING_CHARS)
        and isinstance(line_start, int) and not isinstance(line_start, bool) and 0 < line_start <= 10_000_000
        and isinstance(line_end, int) and not isinstance(line_end, bool) and line_start <= line_end <= 10_000_000
        and isinstance(content_hash, str) and bool(_HEX_64.fullmatch(content_hash))
        and (hash_alias is None or isinstance(hash_alias, str) and hash_alias.lower() == content_hash.lower())
    )
    if not valid:
        diagnostics.append(_diagnostic("source_ref_invalid", field=field, message="Source ref failed provenance validation."))
        return None
    return {
        "source_id": source_id,
        "vault_id": vault_id,
        "note_id": note_id,
        "path": path.replace("\\", "/"),
        "heading": heading,
        "line_start": line_start,
        "line_end": line_end,
        "content_hash": content_hash.lower(),
    }


def _normalise_target(payload: Mapping[str, Any], diagnostics: list[CaptureDiagnostic]) -> tuple[str, str | None]:
    action = payload.get("target_action")
    path = payload.get("target_path")
    target = payload.get("target")
    if target is not None:
        if not isinstance(target, Mapping) or set(target) - {"action", "path"}:
            diagnostics.append(_diagnostic("target_invalid", field="target", message="Target must contain only action and path."))
        else:
            nested_action = target.get("action")
            nested_path = target.get("path")
            if action is not None and nested_action != action:
                diagnostics.append(_diagnostic("target_invalid", field="target_action", message="Target action is contradictory."))
            if path is not None and nested_path != path:
                diagnostics.append(_diagnostic("target_invalid", field="target_path", message="Target path is contradictory."))
            action = nested_action if action is None else action
            path = nested_path if path is None else path
    if action is None:
        action = "create"
    if action != "create":
        diagnostics.append(_diagnostic("target_invalid", field="target_action", message="P5 only creates unapproved inbox drafts."))
    if path is not None:
        if not isinstance(path, str) or not _valid_inbox_target(path):
            diagnostics.append(_diagnostic("target_invalid", field="target_path", message="Draft target must be a relative 00-Inbox Markdown path."))
            path = None
    return str(action), path


def _normalise_strings(
    value: Any,
    field: str,
    max_count: int,
    max_chars: int,
    diagnostics: list[CaptureDiagnostic],
) -> list[str] | None:
    if not isinstance(value, list) or len(value) > max_count:
        diagnostics.append(_diagnostic("candidate_schema_invalid", field=field, message="Field must be a bounded list."))
        return None
    items: list[str] = []
    for index, item in enumerate(value):
        text = _bounded_string(item, f"{field}[{index}]", max_chars, diagnostics)
        if text is not None:
            items.append(text)
    return items


def _bounded_string(
    value: Any,
    field: str,
    max_chars: int,
    diagnostics: list[CaptureDiagnostic],
    *,
    required: bool = True,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        diagnostics.append(_diagnostic("candidate_schema_invalid", field=field, message="Field must be non-empty text."))
        return None
    if len(value) > max_chars:
        diagnostics.append(_diagnostic("field_oversize", field=field, message="Field exceeds its size limit."))
        return None
    return value.strip()


def _source_ref_is_within(candidate: Mapping[str, Any], registered: Mapping[str, Any]) -> bool:
    identity = ("source_id", "vault_id", "note_id", "path", "content_hash")
    if any(candidate[key] != registered[key] for key in identity):
        return False
    if candidate["heading"] is not None and candidate["heading"] != registered["heading"]:
        return False
    return registered["line_start"] <= candidate["line_start"] <= candidate["line_end"] <= registered["line_end"]


def _safe_relative_path(value: str) -> bool:
    return (
        not value.startswith(("/", "~"))
        and "\\" not in value
        and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _persist_inbox_draft(draft: Mapping[str, Any], vault: VaultRegistration) -> None:
    """Materialize a non-personal draft in 00-Inbox without formal indexing.

    This is deliberately opt-in: callers must provide a synthetic/testable work
    vault, and the target is constrained to the one-level inbox path emitted by
    P5.  The file is an unapproved review artifact; P2/P3 never scan 00-Inbox.
    """

    if vault.is_personal or vault.vault_id != "work":
        raise ValueError("Inbox draft materialization requires a non-personal work vault")
    raw_target = draft.get("target_path")
    content = draft.get("content")
    if not isinstance(raw_target, str) or not _valid_inbox_target(raw_target):
        raise ValueError("Inbox draft target is invalid")
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError("Inbox draft content is invalid")

    root = Path(vault.path)
    try:
        root_stat = root.lstat()
        inbox = root / "00-Inbox"
        inbox_stat = inbox.lstat()
    except OSError as error:
        raise RuntimeError("Inbox draft destination is unavailable") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("Inbox draft root must be a real directory")
    if stat.S_ISLNK(inbox_stat.st_mode) or not stat.S_ISDIR(inbox_stat.st_mode):
        raise ValueError("Inbox draft directory must be a real directory")

    valid, _message, resolved = validate_vault_path(vault, raw_target, require_formal=False)
    target = root.joinpath(*Path(raw_target).parts)
    if not valid or resolved is None or resolved != target.resolve(strict=False):
        raise ValueError("Inbox draft target is outside the synthetic work vault")

    try:
        existing_stat = target.lstat()
    except FileNotFoundError:
        existing_stat = None
    except OSError as error:
        raise RuntimeError("Inbox draft target is unavailable") from error
    if existing_stat is not None:
        if stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode):
            raise ValueError("Inbox draft target is not a regular file")
        try:
            existing = _read_regular_file(target)
        except OSError as error:
            raise RuntimeError("Inbox draft target cannot be read") from error
        if hashlib.sha256(existing).hexdigest() == hashlib.sha256(content.encode("utf-8")).hexdigest():
            return
        raise ValueError("Inbox draft target already contains different content")

    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".draft-tmp", dir=inbox)
        temporary = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
            temporary.unlink()
            temporary = None
        except FileExistsError:
            # A concurrent identical capture is idempotent; never overwrite a
            # different artifact that won the race.
            try:
                existing = _read_regular_file(target)
            except OSError as error:
                raise ValueError("Inbox draft target appeared with different content") from error
            if existing != content.encode("utf-8"):
                raise ValueError("Inbox draft target appeared with different content")
            temporary.unlink()
            temporary = None
        directory_fd = os.open(inbox, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except ValueError:
        raise
    except OSError as error:
        raise RuntimeError("Unable to persist inbox draft") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _read_regular_file(path: Path) -> bytes:
    """Read a regular file without following a leaf symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            data = stream.read()
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("file identity changed during read")
        return data
    except BaseException:
        # fdopen owns the descriptor after successful construction; otherwise
        # close the raw descriptor before propagating the read failure.
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _valid_inbox_target(value: str) -> bool:
    return (
        len(value) <= MAX_TARGET_PATH_CHARS
        and _safe_relative_path(value)
        and value.startswith("00-Inbox/")
        and value.endswith(".md")
        and value.count("/") == 1
    )


def _approved_persistence_decision(value: Mapping[str, Any] | None) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        set(value) == {"status", "approved_by", "approved_at", "scope"}
        and value.get("status") == "approved"
        and isinstance(value.get("approved_by"), str)
        and bool(value["approved_by"].strip())
        and isinstance(value.get("approved_at"), str)
        and bool(value["approved_at"].strip())
        and value.get("scope") == "candidate_content"
    )


def _validation_level(evidence: Iterable[Mapping[str, str]]) -> str:
    kinds = {item["kind"] for item in evidence}
    if kinds == {"agent_reported"}:
        return "agent_reported"
    if kinds == {"externally_observed"}:
        return "externally_observed"
    return "mixed"


def _safe_provenance(value: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    diagnostics: list[CaptureDiagnostic] = []
    return _normalise_source_refs(value, "source_manifest", diagnostics) if value is not None else []


def _capture_provenance(value: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep only bounded non-personal refs in persisted capture metadata.

    Personal refs may be needed transiently for candidate validation and may
    be retained in a P6-approved draft.  They must never cross into the
    captures table, regardless of the capture outcome.
    """

    return [ref for ref in _safe_provenance(value) if ref["vault_id"] == "work"]


def _incomplete(
    store: DraftStore,
    task_id: str,
    turn_id: str,
    diagnostics: list[CaptureDiagnostic],
    personal_context_used: bool,
    provenance: list[Mapping[str, Any]],
    *,
    candidate_hash: str | None = None,
) -> CaptureResult:
    deduplicated = tuple(_deduplicate_diagnostics(diagnostics))
    store.record_capture(
        task_id=task_id,
        turn_id=turn_id,
        candidate_hash=candidate_hash,
        status="capture_incomplete",
        diagnostics=[item.to_dict() for item in deduplicated],
        provenance=_capture_provenance(provenance),
        personal_context_used=personal_context_used,
    )
    return CaptureResult("capture_incomplete", None, None, candidate_hash, deduplicated)


def _deduplicate_diagnostics(items: Iterable[CaptureDiagnostic]) -> list[CaptureDiagnostic]:
    seen: set[tuple[str, str | None]] = set()
    result: list[CaptureDiagnostic] = []
    for item in items:
        key = (item.code, item.field)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _diagnostic(code: str, *, field: str | None = None, message: str) -> CaptureDiagnostic:
    return CaptureDiagnostic(code=code, field=field, message=message)


def _valid_identity(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _title_from_summary(summary: str) -> str:
    title = summary.splitlines()[0].strip().rstrip(".!?。！？")
    return title[:MAX_TITLE_CHARS] or "Knowledge Candidate"


def _default_target_path(title: str, candidate_hash: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:80]
    slug = slug or "knowledge-candidate"
    return f"00-Inbox/{slug}-{candidate_hash[:12]}.md"


__all__ = [
    "CANDIDATE_BLOCK_END",
    "CANDIDATE_BLOCK_START",
    "COMMENT_CANDIDATE_END",
    "COMMENT_CANDIDATE_START",
    "CaptureDiagnostic",
    "CaptureResult",
    "CandidateParse",
    "MAX_CANDIDATE_BLOCK_CHARS",
    "MAX_RESULT_CHARS",
    "capture_result",
    "parse_candidate_block",
    "validate_candidate_payload",
]
