"""Provenance-preserving source reads for the knowledge retrieval layer.

The SQLite index is derived state.  A source read therefore always verifies the
current Markdown bytes against the hash observed by the index (or by an
in-process personal-vault search) before returning text to a caller.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Mapping

from .config import VaultRegistration
from .index import KnowledgeIndex
from .scope import validate_vault_path


class ProvenanceError(RuntimeError):
    """Base class for fail-closed source and provenance errors."""


class AccessDeniedError(ProvenanceError):
    """The task has no in-process authorization for the requested source."""


class SourceNotFoundError(ProvenanceError):
    """The requested source is not registered or no longer exists."""


class SourceChangedError(ProvenanceError):
    """The source bytes differ from the hash used to produce the source ref."""

    def __init__(self, source: SourceRef, expected_hash: str, actual_hash: str):
        self.source = source
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        super().__init__(
            f"Source changed since indexing: {source.vault_id}/{source.path} "
            f"(expected {expected_hash}, observed {actual_hash})"
        )


class SourceReadError(ProvenanceError):
    """The source could not be safely decoded or read."""


@dataclass(frozen=True)
class SourceRef:
    """A stable, portable pointer to a bounded source range."""

    source_id: str
    vault_id: str
    note_id: str
    path: str
    heading: str | None
    line_start: int
    line_end: int
    content_hash: str

    @property
    def hash(self) -> str:
        """Compatibility alias for consumers that call the field ``hash``."""

        return self.content_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "vault_id": self.vault_id,
            "note_id": self.note_id,
            "path": self.path,
            "heading": self.heading,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "content_hash": self.content_hash,
            "hash": self.content_hash,
        }

    @classmethod
    def from_mapping(cls, value: SourceRef | Mapping[str, Any]) -> SourceRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("source must be a SourceRef or mapping")
        try:
            content_hash = value.get("content_hash") or value["hash"]
            return cls(
                source_id=str(value["source_id"]),
                vault_id=str(value["vault_id"]),
                note_id=str(value["note_id"]),
                path=str(value["path"]),
                heading=str(value["heading"]) if value.get("heading") is not None else None,
                line_start=int(value["line_start"]),
                line_end=int(value["line_end"]),
                content_hash=str(content_hash),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("source mapping is missing valid provenance fields") from error


@dataclass(frozen=True)
class SourceRead:
    """Text returned only after a source hash and access check succeed."""

    source: SourceRef
    content: str
    truncated: bool = False
    source_content_untrusted: bool = True

    def to_dict(self) -> dict[str, Any]:
        result = {
            "source_ref": self.source.to_dict(),
            "provenance": self.source.to_dict(),
            "content": self.content,
            "snippet": self.content,
            "truncated": self.truncated,
            "source_content_untrusted": self.source_content_untrusted,
            "untrusted_data": self.source_content_untrusted,
        }
        result.update(
            {
                "source_id": self.source.source_id,
                "vault_id": self.source.vault_id,
                "note_id": self.source.note_id,
                "path": self.source.path,
                "heading": self.source.heading,
                "line_start": self.source.line_start,
                "line_end": self.source.line_end,
                "content_hash": self.source.content_hash,
                "hash": self.source.content_hash,
            }
        )
        return result


def _source_id(vault_id: str, note_id: str) -> str:
    return hashlib.sha256(f"{vault_id}\0{note_id}".encode("utf-8")).hexdigest()


class SourceReader:
    """Read registered work sources and explicitly granted personal sources."""

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        work_vault: VaultRegistration,
        personal_vault: VaultRegistration | None = None,
        grant_manager: Any | None = None,
    ):
        self.index = index
        self.work_vault = work_vault
        self.personal_vault = personal_vault
        self.grant_manager = grant_manager

    def _work_row(self, source_id: str):
        if not self.index._database_healthy():
            raise SourceNotFoundError("Derived knowledge index is unavailable")
        connection = None
        try:
            connection = self.index._connect()
            row = connection.execute(
                """
                SELECT source_id, vault_id, note_id, path, content_hash
                FROM sources WHERE source_id = ? AND vault_id = ?
                """,
                (source_id, self.work_vault.vault_id),
            ).fetchone()
            if row is None:
                raise SourceNotFoundError(f"Unknown indexed source: {source_id}")
            return row
        except SourceNotFoundError:
            raise
        except Exception as error:
            raise SourceNotFoundError(f"Unable to read source metadata: {error}") from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _line_slice(content: str, line_start: int, line_end: int) -> tuple[str, int, int]:
        if line_start <= 0 or line_end <= 0 or line_start > line_end:
            raise ValueError("line range must be positive and ordered")
        lines = content.splitlines(keepends=True)
        if not lines:
            return "", 1, 1
        start = min(line_start, len(lines))
        end = min(line_end, len(lines))
        return "".join(lines[start - 1 : end]), start, end

    def _read_checked(
        self,
        source: SourceRef,
        path: Path,
        *,
        expected_hash: str,
        line_start: int,
        line_end: int,
        max_chars: int | None,
    ) -> SourceRead:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise SourceReadError(f"Unable to read source {source.path}: {error}") from error
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash:
            raise SourceChangedError(source, expected_hash, actual_hash)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceReadError(f"Source is not valid UTF-8: {source.path}") from error

        selected, actual_start, actual_end = self._line_slice(content, line_start, line_end)
        truncated = False
        if max_chars is not None:
            if max_chars <= 0:
                raise ValueError("max_chars must be positive")
            if len(selected) > max_chars:
                selected = selected[:max_chars]
                truncated = True
        returned_ref = SourceRef(
            source_id=source.source_id,
            vault_id=source.vault_id,
            note_id=source.note_id,
            path=source.path,
            heading=source.heading,
            line_start=actual_start,
            line_end=actual_end,
            content_hash=actual_hash,
        )
        return SourceRead(returned_ref, selected, truncated=truncated)

    def _read_work(
        self,
        source: SourceRef,
        *,
        line_start: int,
        line_end: int,
        max_chars: int | None,
    ) -> SourceRead:
        row = self._work_row(source.source_id)
        if source.vault_id != row["vault_id"] or source.note_id != row["note_id"] or source.path != row["path"]:
            raise SourceNotFoundError("Source provenance does not match the indexed source")
        indexed_hash = str(row["content_hash"])
        if source.content_hash != indexed_hash:
            raise SourceChangedError(source, source.content_hash, indexed_hash)
        valid, message, resolved = validate_vault_path(self.work_vault, row["path"], require_formal=True)
        if not valid or resolved is None or not resolved.is_file():
            raise SourceNotFoundError(f"Source is outside the registered work scope: {message}")
        return self._read_checked(
            source,
            resolved,
            expected_hash=indexed_hash,
            line_start=line_start,
            line_end=line_end,
            max_chars=max_chars,
        )

    def _read_personal(
        self,
        source: SourceRef,
        task_id: str,
        *,
        line_start: int,
        line_end: int,
        max_chars: int | None,
    ) -> SourceRead:
        if self.personal_vault is None or self.grant_manager is None:
            raise AccessDeniedError("Personal source access requires an in-process TaskGrantManager grant")
        if source.vault_id != self.personal_vault.vault_id:
            raise AccessDeniedError("Source is not in the registered personal vault")
        if source.source_id != _source_id(source.vault_id, source.note_id):
            raise SourceNotFoundError("Source provenance has an invalid personal source ID")
        if not self.grant_manager.check_access(task_id, source.path, self.personal_vault):
            raise AccessDeniedError("Task has no active grant for this personal source")
        valid, message, resolved = validate_vault_path(self.personal_vault, source.path, require_formal=False)
        if not valid or resolved is None or not resolved.is_file():
            raise SourceNotFoundError(f"Personal source is outside scope: {message}")
        expected_hash = source.content_hash
        if not expected_hash:
            raise SourceChangedError(source, "missing", "unknown")
        return self._read_checked(
            source,
            resolved,
            expected_hash=expected_hash,
            line_start=line_start,
            line_end=line_end,
            max_chars=max_chars,
        )

    def read_source(
        self,
        source: SourceRef | Mapping[str, Any] | str,
        *,
        task_id: str,
        line_start: int | None = None,
        line_end: int | None = None,
        expected_hash: str | None = None,
        max_chars: int | None = None,
    ) -> SourceRead:
        """Read a source range, failing closed on missing grants or changed bytes."""

        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required for source reads")
        if isinstance(source, str):
            source = self._source_ref_from_id(source)
        source_ref = SourceRef.from_mapping(source)
        if expected_hash is not None and source_ref.content_hash != expected_hash:
            raise SourceChangedError(source_ref, expected_hash, source_ref.content_hash)
        start = line_start if line_start is not None else source_ref.line_start
        end = line_end if line_end is not None else source_ref.line_end
        if source_ref.vault_id == self.work_vault.vault_id:
            return self._read_work(source_ref, line_start=start, line_end=end, max_chars=max_chars)
        if self.personal_vault is not None and source_ref.vault_id == self.personal_vault.vault_id:
            return self._read_personal(
                source_ref,
                task_id,
                line_start=start,
                line_end=end,
                max_chars=max_chars,
            )
        raise AccessDeniedError("Source vault is not registered for this reader")

    def _source_ref_from_id(self, source_id: str) -> SourceRef:
        row = self._work_row(source_id)
        return SourceRef(
            source_id=str(row["source_id"]),
            vault_id=str(row["vault_id"]),
            note_id=str(row["note_id"]),
            path=str(row["path"]),
            heading=None,
            line_start=1,
            # A source-id-only read means the complete note.  The line slicer
            # clamps this sentinel to the actual last line.
            line_end=2**31 - 1,
            content_hash=str(row["content_hash"]),
        )


__all__ = [
    "AccessDeniedError",
    "ProvenanceError",
    "SourceChangedError",
    "SourceNotFoundError",
    "SourceRead",
    "SourceReadError",
    "SourceReader",
    "SourceRef",
]
