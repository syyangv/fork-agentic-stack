"""Human approval, conflict-safe Markdown apply, and recovery for P6.

This module is intentionally not imported by the Paseo plugin.  Its public
entry points are a local human-review seam; the ``human_approval`` argument is
an explicit call-site guard, not an operating-system security boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from threading import RLock
from typing import Any, Callable, Iterator, Mapping

from .config import FORMAL_DIR_ALLOWLIST, VaultRegistration
from .drafts import DraftStore


JOURNAL_VERSION = 1
ALLOWED_TARGET_DIRS = frozenset(("00-Inbox", *FORMAL_DIR_ALLOWLIST))
EXCLUDED_TARGET_COMPONENTS = frozenset(
    {"90-Archive", ".obsidian", "config", "configs", "attachments", "_attachments"}
)
SUPPORTED_ACTIONS = frozenset({"create", "update", "modify", "single_file_update"})
MAX_APPLY_CONTENT_CHARS = 200_000
HASH_LENGTH = 64

_PROCESS_LOCK_GUARD = RLock()
_PROCESS_LOCKS: dict[str, RLock] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(content: str) -> str:
    return _sha256_bytes(content.encode("utf-8"))


def _is_hash(value: Any, *, allow_none: bool = False) -> bool:
    return allow_none and value is None or isinstance(value, str) and len(value) == HASH_LENGTH and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


class ApprovalError(RuntimeError):
    """Base class for fail-closed review/apply failures."""

    code = "approval_error"


class ApprovalRequiredError(ApprovalError):
    code = "human_approval_required"


class DraftNotFoundError(ApprovalError):
    code = "draft_not_found"


class TerminalDraftError(ApprovalError):
    code = "draft_terminal"


class UnsupportedActionError(ApprovalError):
    code = "unsupported_action"


class TargetSafetyError(ApprovalError):
    code = "unsafe_target"


class ApplyConflictError(ApprovalError):
    code = "apply_conflict"

    def __init__(self, message: str, *, target_path: str | None = None):
        super().__init__(message)
        self.target_path = target_path


class JournalError(ApprovalError):
    code = "journal_error"


@dataclass(frozen=True)
class ApplyResult:
    """Bounded result returned after an accepted apply or idempotent replay."""

    success: bool
    status: str
    draft_id: str
    target_path: str
    target_action: str
    preimage_hash: str | None
    desired_hash: str
    post_write_hash: str | None
    idempotent: bool = False
    index_updated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "draft_id": self.draft_id,
            "target_path": self.target_path,
            "target_action": self.target_action,
            "preimage_hash": self.preimage_hash,
            "desired_hash": self.desired_hash,
            "post_write_hash": self.post_write_hash,
            "idempotent": self.idempotent,
            "index_updated": self.index_updated,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class ApplyJournal:
    """Append-only, fsynced journal whose records contain hashes, not content."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._lock = RLock()

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise JournalError("Journal record must be an object")
        bounded = dict(record)
        bounded.setdefault("journal_version", JOURNAL_VERSION)
        bounded.setdefault("recorded_at", _now_iso())
        try:
            encoded = (json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        except (TypeError, ValueError) as error:
            raise JournalError("Journal record is not JSON serializable") from error
        if len(encoded) > 16_384:
            raise JournalError("Journal record exceeds the bounded metadata limit")

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self.path, flags | nofollow, 0o600)
            except OSError as error:
                raise JournalError("Unable to open apply journal") from error
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "ab", closefd=True) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                raise JournalError("Unable to persist apply journal") from error
        return bounded

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise JournalError("Apply journal is not a regular file")
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise JournalError("Unable to read apply journal") from error
        records: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise JournalError("Apply journal contains invalid JSON") from error
            if not isinstance(item, dict) or item.get("journal_version") != JOURNAL_VERSION:
                raise JournalError("Apply journal contains an unsupported record")
            records.append(item)
        return records

    def latest(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in self.records():
            operation_id = record.get("operation_id")
            if isinstance(operation_id, str) and operation_id:
                latest[operation_id] = record
        return latest


@contextmanager
def _target_lock(target: Path, lock_root: Path) -> Iterator[None]:
    """Serialize this target in-process and across cooperating processes."""

    key = str(target)
    with _PROCESS_LOCK_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, RLock())
    with process_lock:
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = lock_root / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise ApprovalError("Unable to acquire per-target apply lock") from error
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class ApprovalApplier:
    """Apply only explicitly accepted drafts to one synthetic work vault."""

    def __init__(
        self,
        work_vault: VaultRegistration,
        draft_store: DraftStore,
        journal_path: str | Path,
        *,
        index: Any | None = None,
        index_refresh: Callable[[str], Any] | None = None,
        event_hook: Callable[[str], Any] | None = None,
    ):
        if work_vault.is_personal:
            raise TargetSafetyError("P6 apply accepts only a non-personal work vault")
        self.work_vault = work_vault
        self.draft_store = draft_store
        self.journal = ApplyJournal(journal_path)
        self.index = index
        self.index_refresh = index_refresh
        self.event_hook = event_hook
        self._root_raw = Path(work_vault.path)
        self._root = self._root_raw.resolve()
        if self.journal.path == self._root or self._root in self.journal.path.parents:
            raise TargetSafetyError("Apply journal must be outside the work-vault Markdown tree")
        self._lock_root = self.journal.path.parent / "locks"

    def apply(
        self,
        draft_id: str,
        *,
        draft_hash: str,
        target_hash: str | None,
        content: str | None = None,
        new_content: str | None = None,
        target_path: str | None = None,
        human_approval: bool | Mapping[str, Any] | None = None,
        approved: bool | None = None,
        approved_by: str | None = None,
    ) -> ApplyResult:
        """Accept and apply a draft after an explicit human-shaped confirmation.

        ``draft_hash`` always binds the exact bytes to be written.  Therefore
        an edited draft must be submitted with the hash of the edited content,
        not merely the original P5 draft hash.
        """

        if target_path is None and isinstance(human_approval, Mapping):
            selected_by_human = human_approval.get("target_path")
            if isinstance(selected_by_human, str):
                target_path = selected_by_human
        self._require_human_approval(
            human_approval,
            approved=approved,
            decision="accept",
            draft_hash=draft_hash,
            target_hash=target_hash,
            target_path=target_path,
        )
        if content is not None and new_content is not None and content != new_content:
            raise ApprovalError("Provide only one edited content value")
        selected_content = new_content if new_content is not None else content
        draft = self.draft_store.get_draft(draft_id)
        if draft is None:
            raise DraftNotFoundError(f"Unknown draft: {draft_id}")
        status = str(draft.get("status"))
        if status == "rejected":
            raise TerminalDraftError("Rejected drafts are terminal and cannot be applied")
        if status not in {"draft", "accepted"}:
            raise TerminalDraftError(f"Draft status cannot be applied: {status}")

        draft_action = str(draft.get("target_action", ""))
        if draft_action == "create":
            if target_path is None:
                raise TargetSafetyError("New-note approval requires an explicit formal target path")
            selected_target_path = target_path
        else:
            if target_path is not None and target_path != draft.get("target_path"):
                raise TargetSafetyError("Single-file updates cannot be retargeted during approval")
            selected_target_path = str(draft.get("target_path", ""))

        effective_content = draft["content"] if selected_content is None else selected_content
        if not isinstance(effective_content, str):
            raise ApprovalError("Approved Markdown content must be text")
        if len(effective_content) > MAX_APPLY_CONTENT_CHARS or "\x00" in effective_content:
            raise ApprovalError("Approved Markdown content exceeds safety limits")
        desired_hash = _sha256_text(effective_content)
        if not _is_hash(draft_hash) or draft_hash.lower() != desired_hash:
            raise ApprovalError("Approved draft hash does not match the exact content")
        if isinstance(human_approval, Mapping):
            confirmed = human_approval.get("confirmed_draft_hash")
            if confirmed is not None and str(confirmed).lower() != desired_hash:
                raise ApprovalError("Human confirmation does not match approved content")
        target, action = self._validate_target(draft, target_path=selected_target_path)
        applied_target_path = str(selected_target_path)
        expected_base = draft.get("target_base_hash")
        if action == "create":
            if target_hash not in (None, ""):
                raise ApplyConflictError("New-note apply requires an absent target hash", target_path=applied_target_path)
            if expected_base not in (None, ""):
                raise ApplyConflictError("New-note draft unexpectedly has a target base hash", target_path=applied_target_path)
        else:
            if not _is_hash(expected_base) or not _is_hash(target_hash):
                raise ApplyConflictError("Single-file update requires a stored and confirmed base hash", target_path=applied_target_path)
            if str(expected_base).lower() != str(target_hash).lower():
                raise ApplyConflictError("Confirmed target hash differs from the stored target base hash", target_path=applied_target_path)

        with _target_lock(target, self._lock_root):
            if status == "accepted":
                return self._repeat_accepted(draft, target, action, desired_hash, target_hash, applied_target_path)

            operation_id = self._operation_id(draft_id, action, applied_target_path, draft_hash, target_hash, desired_hash)
            previous = self.journal.latest().get(operation_id)
            if previous is not None:
                if previous.get("state") in {"committed", "indexed", "finalized"}:
                    return self._repeat_accepted(draft, target, action, desired_hash, target_hash, applied_target_path)
                if previous.get("state") in {"write_intent", "written"}:
                    recovered = self._recover_one(previous)
                    if recovered is not None:
                        return recovered
                    raise ApplyConflictError("An unfinished apply journal entry requires recovery", target_path=applied_target_path)

            preimage_hash = self._check_expected_target(target, action, target_hash, initial=True)
            base_record = {
                "operation_id": operation_id,
                "draft_id": draft_id,
                "target_path": applied_target_path,
                "target_action": action,
                "target_hash": target_hash,
                "preimage_hash": preimage_hash,
                "desired_hash": desired_hash,
                "draft_hash": desired_hash,
            }
            self.journal.append({**base_record, "state": "prepared"})
            self._hook("prepared")
            temp_path: Path | None = None
            try:
                temp_path = self._write_temp(target, effective_content)
                self.journal.append({**base_record, "state": "write_intent"})
                self._hook("write_intent")
                # This is the final target check immediately before the target write.
                self._check_expected_target(target, action, target_hash, initial=False)
                current = self.draft_store.get_draft(draft_id)
                if current is None or current.get("status") != "draft":
                    raise TerminalDraftError("Draft changed state before its approved write")
                self._hook("before_target_write")
                self._install_temp(temp_path, target, action)
                temp_path = None
                self._hook("after_target_write")
                post_write_hash = self._hash_target(target, target_path=applied_target_path)
                if post_write_hash != desired_hash:
                    raise ApplyConflictError("Post-write hash did not match desired content", target_path=applied_target_path)
                self.journal.append({**base_record, "state": "written", "post_write_hash": post_write_hash})
                self._hook("written")
                self._commit_accept(
                    draft,
                    operation_id=operation_id,
                    draft_hash=desired_hash,
                    target_hash=target_hash,
                    desired_hash=desired_hash,
                    preimage_hash=preimage_hash,
                    post_write_hash=post_write_hash,
                    approved_by=approved_by or self._approval_actor(human_approval),
                )
                self.journal.append({**base_record, "state": "committed", "post_write_hash": post_write_hash})
                self._hook("committed")
                index_updated = self._refresh_index(applied_target_path)
                self.journal.append(
                    {**base_record, "state": "indexed" if index_updated else "finalized", "post_write_hash": post_write_hash}
                )
                return ApplyResult(True, "accepted", draft_id, applied_target_path, action, preimage_hash, desired_hash, post_write_hash, index_updated=index_updated)
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass

    def reject(
        self,
        draft_id: str,
        *,
        reason: str,
        human_approval: bool | Mapping[str, Any] | None = None,
        approved: bool | None = None,
        rejected_by: str | None = None,
    ) -> dict[str, Any]:
        """Terminally reject a draft, retaining provenance and bounded reason."""

        self._require_human_approval(human_approval, approved=approved, decision="reject")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2_000:
            raise ApprovalError("A bounded rejection reason is required")
        draft = self.draft_store.get_draft(draft_id)
        if draft is None:
            raise DraftNotFoundError(f"Unknown draft: {draft_id}")
        if draft["status"] == "accepted":
            raise TerminalDraftError("Accepted drafts are terminal")
        review_id = "reject_" + hashlib.sha256(f"{draft_id}\0{reason}".encode("utf-8")).hexdigest()
        stored = self.draft_store.update_draft_status(
            draft_id,
            "rejected",
            expected_status="draft",
            review={
                "review_id": review_id,
                "decision": "reject",
                "draft_hash": draft["draft_hash"],
                "reason": reason.strip(),
                "approved_by": rejected_by or self._approval_actor(human_approval),
                "state": "rejected",
            },
        )
        return {
            "success": True,
            "status": "rejected",
            "draft_id": draft_id,
            "reason": reason.strip(),
            "draft_hash": stored["draft_hash"],
            "target_path": stored["target_path"],
        }

    def recover(self) -> list[dict[str, Any]]:
        """Finalize only interrupted writes already proven by desired hash."""

        results: list[dict[str, Any]] = []
        for record in self.journal.latest().values():
            if record.get("state") not in {"write_intent", "written", "committed"}:
                continue
            result = self._recover_one(record)
            if result is not None:
                results.append(result.to_dict())
        return results

    def _recover_one(self, record: Mapping[str, Any]) -> ApplyResult | None:
        draft_id = str(record.get("draft_id", ""))
        target_path = str(record.get("target_path", ""))
        draft = self.draft_store.get_draft(draft_id)
        if draft is None:
            raise JournalError("Apply journal references an unknown draft")
        target, action = self._validate_target(draft, target_path=target_path)
        if action != "create" and target_path != str(draft["target_path"]):
            raise JournalError("Apply journal target does not match draft target")
        desired_hash = str(record.get("desired_hash", ""))
        if not _is_hash(desired_hash):
            raise JournalError("Apply journal desired hash is invalid")
        with _target_lock(target, self._lock_root):
            state = str(record.get("state"))
            if state == "committed" and draft["status"] == "accepted":
                try:
                    observed = self._hash_target(target, target_path=target_path)
                except ApplyConflictError:
                    self.journal.append({**dict(record), "state": "conflict", "reason": "accepted_target_changed"})
                    raise
                if observed != desired_hash:
                    self.journal.append({**dict(record), "state": "conflict", "reason": "accepted_hash_missing", "observed_hash": observed})
                    raise ApplyConflictError("Accepted target no longer has its accepted hash", target_path=target_path)
                index_updated = self._refresh_index(target_path)
                self.journal.append({**dict(record), "state": "indexed" if index_updated else "finalized"})
                return ApplyResult(True, "accepted", draft_id, target_path, action, record.get("preimage_hash"), desired_hash, record.get("post_write_hash"), idempotent=True, index_updated=index_updated)
            if draft["status"] == "rejected":
                self.journal.append({**dict(record), "state": "conflict", "reason": "draft_rejected"})
                raise ApplyConflictError("Interrupted apply belongs to a rejected draft", target_path=target_path)
            if state == "committed" and draft["status"] != "draft":
                raise JournalError("Apply journal and draft state disagree")
            try:
                actual_hash = self._hash_target(target, target_path=target_path)
            except ApplyConflictError:
                self.journal.append({**dict(record), "state": "conflict", "reason": "target_missing_or_unsafe"})
                raise
            if actual_hash != desired_hash:
                self.journal.append(
                    {**dict(record), "state": "conflict", "reason": "desired_hash_not_present", "observed_hash": actual_hash}
                )
                raise ApplyConflictError("Recovery found a target different from the desired hash", target_path=target_path)
            if state == "prepared":
                self.journal.append({**dict(record), "state": "conflict", "reason": "write_not_intended"})
                raise ApplyConflictError("Recovery cannot infer a write from a prepared-only entry", target_path=target_path)
            post_write_hash = actual_hash
            self._commit_accept(
                draft,
                operation_id=str(record["operation_id"]),
                draft_hash=str(record.get("draft_hash") or desired_hash),
                target_hash=record.get("target_hash"),
                desired_hash=desired_hash,
                preimage_hash=record.get("preimage_hash"),
                post_write_hash=post_write_hash,
                approved_by="recovered-human-approval",
            )
            committed = {**dict(record), "state": "committed", "post_write_hash": post_write_hash}
            self.journal.append(committed)
            index_updated = self._refresh_index(target_path)
            self.journal.append({**committed, "state": "indexed" if index_updated else "finalized"})
            return ApplyResult(True, "accepted", draft_id, target_path, action, record.get("preimage_hash"), desired_hash, post_write_hash, idempotent=True, index_updated=index_updated)

    def _commit_accept(
        self,
        draft: Mapping[str, Any],
        *,
        operation_id: str,
        draft_hash: str,
        target_hash: str | None,
        desired_hash: str,
        preimage_hash: str | None,
        post_write_hash: str,
        approved_by: str,
    ) -> None:
        existing = self.draft_store.list_reviews(draft_id=str(draft["draft_id"]))
        for review in existing:
            if review.get("decision") == "accept" and review.get("desired_hash") not in {None, desired_hash}:
                raise ApplyConflictError("Draft already has a different accepted content hash", target_path=str(draft["target_path"]))
        self.draft_store.update_draft_status(
            str(draft["draft_id"]),
            "accepted",
            expected_status="draft",
            review={
                "review_id": "accept_" + operation_id,
                "decision": "accept",
                "draft_hash": draft_hash,
                "target_hash": target_hash,
                "desired_hash": desired_hash,
                "preimage_hash": preimage_hash,
                "post_write_hash": post_write_hash,
                "approved_by": approved_by,
                "state": "committed",
            },
        )

    def _repeat_accepted(
        self,
        draft: Mapping[str, Any],
        target: Path,
        action: str,
        desired_hash: str,
        target_hash: str | None,
        target_path: str,
    ) -> ApplyResult:
        reviews = [r for r in self.draft_store.list_reviews(draft_id=str(draft["draft_id"])) if r.get("decision") == "accept"]
        reviewed_hashes = {str(r.get("desired_hash")) for r in reviews if r.get("desired_hash")}
        if reviewed_hashes and desired_hash not in reviewed_hashes:
            raise TerminalDraftError("Accepted draft cannot be changed to a different content hash")
        actual = self._hash_target(target, target_path=target_path)
        if actual != desired_hash:
            raise ApplyConflictError("Accepted target is no longer at the accepted hash; no overwrite is attempted", target_path=target_path)
        preimage = None if action == "create" else target_hash
        return ApplyResult(True, "accepted", str(draft["draft_id"]), target_path, action, preimage, desired_hash, actual, idempotent=True)

    def _validate_target(self, draft: Mapping[str, Any], *, target_path: str | None = None) -> tuple[Path, str]:
        action = str(draft.get("target_action", ""))
        if action not in SUPPORTED_ACTIONS:
            raise UnsupportedActionError("P6 supports only new or single-file Markdown actions")
        if action != "create":
            action = "update"
        raw = draft.get("target_path") if target_path is None else target_path
        if not isinstance(raw, str) or not raw.strip():
            raise TargetSafetyError("Target path is required")
        if raw != raw.strip() or "\\" in raw:
            raise TargetSafetyError("Target path must be a clean relative POSIX path")
        relative = Path(raw)
        if relative.is_absolute() or raw.startswith("~") or ".." in relative.parts or "." in relative.parts:
            raise TargetSafetyError("Traversal and absolute target paths are forbidden")
        if not relative.parts or relative.suffix.lower() != ".md":
            raise TargetSafetyError("P6 targets must be Markdown files")
        if relative.parts[0] not in ALLOWED_TARGET_DIRS:
            raise TargetSafetyError("Target is outside the supported work-vault directories")
        if relative.parts[0] == "00-Inbox":
            raise TargetSafetyError("Accepted notes must target a formal knowledge directory")
        if any(part in EXCLUDED_TARGET_COMPONENTS or part.startswith(".") for part in relative.parts):
            raise TargetSafetyError("Archive, configuration, attachment, and hidden targets are forbidden")
        root = self._root
        try:
            raw_root_stat = self._root_raw.lstat()
        except OSError as error:
            raise TargetSafetyError("Work vault root is not available") from error
        if stat.S_ISLNK(raw_root_stat.st_mode) or not stat.S_ISDIR(raw_root_stat.st_mode):
            raise TargetSafetyError("Work vault root must be a real directory")
        target = root.joinpath(*relative.parts)
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            try:
                current_stat = current.lstat()
            except FileNotFoundError as error:
                raise TargetSafetyError("Target parent directory does not exist") from error
            except OSError as error:
                raise TargetSafetyError("Unable to inspect target parent directory") from error
            if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                raise TargetSafetyError("Target parent must be a real directory")
        try:
            resolved = target.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise TargetSafetyError("Unable to resolve target path") from error
        if resolved != target:
            raise TargetSafetyError("Symlink targets are forbidden")
        return target, action

    def _check_expected_target(self, target: Path, action: str, expected_hash: str | None, *, initial: bool) -> str | None:
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            if action == "create":
                return None
            raise ApplyConflictError("Single-file target was deleted", target_path=str(target))
        except OSError as error:
            raise ApplyConflictError("Unable to inspect target", target_path=str(target)) from error
        if stat.S_ISLNK(target_stat.st_mode):
            raise ApplyConflictError("Target became a symlink; no overwrite attempted", target_path=str(target))
        if not stat.S_ISREG(target_stat.st_mode):
            raise ApplyConflictError("Target is not a regular file", target_path=str(target))
        actual = self._hash_target(target, target_path=str(target))
        if action == "create":
            raise ApplyConflictError("New-note target already exists", target_path=str(target))
        if actual != expected_hash:
            phase = "initial" if initial else "final"
            raise ApplyConflictError(f"Target hash changed during {phase} apply check", target_path=str(target))
        return actual

    def _hash_target(self, target: Path, *, target_path: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags)
        except FileNotFoundError as error:
            raise ApplyConflictError("Target is missing", target_path=target_path) from error
        except OSError as error:
            raise ApplyConflictError("Target cannot be safely opened", target_path=target_path) from error
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ApplyConflictError("Target is not a regular file", target_path=target_path)
            with os.fdopen(fd, "rb", closefd=True) as stream:
                data = stream.read()
            try:
                current = target.lstat()
            except OSError as error:
                raise ApplyConflictError("Target disappeared during read", target_path=target_path) from error
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ApplyConflictError("Target identity changed during read", target_path=target_path)
            return _sha256_bytes(data)
        except ApplyConflictError:
            raise
        except OSError as error:
            raise ApplyConflictError("Target cannot be read safely", target_path=target_path) from error

    def _write_temp(self, target: Path, content: str) -> Path:
        data = content.encode("utf-8")
        try:
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".apply-tmp", dir=target.parent)
            temp = Path(name)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            return temp
        except OSError as error:
            raise ApprovalError("Unable to prepare restrictive temporary Markdown file") from error

    def _install_temp(self, temp: Path, target: Path, action: str) -> None:
        try:
            if action == "create":
                # Hard-linking the exclusive temp gives create-if-absent
                # semantics; unlike rename, it cannot replace a collision.
                os.link(temp, target)
                temp.unlink()
            else:
                os.replace(temp, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError as error:
            raise ApplyConflictError("New-note target appeared before atomic create", target_path=str(target)) from error
        except OSError as error:
            raise ApprovalError("Atomic Markdown install failed") from error

    def _refresh_index(self, relative_path: str) -> bool:
        try:
            if self.index_refresh is not None:
                self.index_refresh(relative_path)
                return True
            if self.index is None:
                return False
            if hasattr(self.index, "sync_path"):
                self.index.sync_path(self.work_vault, relative_path)
            else:
                # A path-scoped refresh must never silently widen into a
                # whole-vault rebuild or scan.  An explicit index command can
                # repair a missing/corrupt derived database later.
                return False
            return True
        except Exception:
            # Accepted Markdown must remain authoritative even when a derived
            # index refresh is unavailable. Recovery can refresh again later.
            return False

    def _hook(self, event: str) -> None:
        if self.event_hook is not None:
            self.event_hook(event)

    @staticmethod
    def _operation_id(draft_id: str, action: str, target_path: str, draft_hash: str, target_hash: str | None, desired_hash: str) -> str:
        value = "\0".join((draft_id, action, target_path, draft_hash.lower(), target_hash or "", desired_hash.lower()))
        return "apply_" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _approval_actor(approval: bool | Mapping[str, Any] | None) -> str:
        if isinstance(approval, Mapping) and isinstance(approval.get("approved_by"), str) and approval["approved_by"].strip():
            return approval["approved_by"].strip()[:256]
        return "human-review"

    @staticmethod
    def _require_human_approval(
        approval: bool | Mapping[str, Any] | None,
        *,
        approved: bool | None,
        decision: str,
        draft_hash: str | None = None,
        target_hash: str | None = None,
        target_path: str | None = None,
    ) -> None:
        explicit = approved is True or approval is True
        if isinstance(approval, Mapping):
            explicit = approval.get("decision") == decision and approval.get("human", True) is not False
            if draft_hash is not None and approval.get("confirmed_draft_hash") is not None:
                if str(approval["confirmed_draft_hash"]).lower() != str(draft_hash).lower():
                    raise ApprovalRequiredError("Human confirmation does not match the supplied draft hash")
            if target_hash is not None and approval.get("confirmed_target_hash") is not None:
                if str(approval["confirmed_target_hash"]).lower() != str(target_hash).lower():
                    raise ApprovalRequiredError("Human confirmation does not match the supplied target hash")
            if target_path is not None:
                confirmed_target_path = approval.get("target_path")
                if confirmed_target_path != target_path:
                    raise ApprovalRequiredError("Human confirmation does not match the selected target path")
        if not explicit:
            raise ApprovalRequiredError("P6 apply/reject requires an explicit human approval decision")


def review_apply(applier: ApprovalApplier, draft_id: str, **kwargs: Any) -> ApplyResult:
    """Human-only named wrapper for callers that mirror the CLI command."""

    return applier.apply(draft_id, **kwargs)


def review_reject(applier: ApprovalApplier, draft_id: str, **kwargs: Any) -> dict[str, Any]:
    """Human-only named wrapper for callers that mirror the CLI command."""

    return applier.reject(draft_id, **kwargs)


__all__ = [
    "ALLOWED_TARGET_DIRS",
    "ApplyConflictError",
    "ApplyJournal",
    "ApplyResult",
    "ApprovalApplier",
    "ApprovalError",
    "ApprovalRequiredError",
    "DraftNotFoundError",
    "JournalError",
    "TargetSafetyError",
    "TerminalDraftError",
    "UnsupportedActionError",
    "review_apply",
    "review_reject",
]
