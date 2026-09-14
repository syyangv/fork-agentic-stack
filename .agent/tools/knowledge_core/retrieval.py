"""Deterministic, provenance-aware retrieval over the P2 derived index."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from .chunks import MarkdownChunk, chunk_markdown
from .config import VaultRegistration
from .grants import TaskGrantManager
from .index import KnowledgeIndex
from .parser import parse_markdown_bytes
from .provenance import AccessDeniedError, SourceRef
from .scope import validate_vault_path


class RetrievalError(RuntimeError):
    """Base class for retrieval failures."""


class IndexUnavailableError(RetrievalError):
    """The derived index is missing, corrupt, or cannot be queried."""


class InvalidQueryError(RetrievalError):
    """The query cannot be safely searched."""


@dataclass
class SearchResponse:
    """A list-compatible response that preserves no-result/error distinction."""

    status: str
    results: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, str] | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "no_results"}

    def __iter__(self):
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, key):
        if isinstance(key, int) or isinstance(key, slice):
            return self.results[key]
        if isinstance(key, str):
            return self.to_dict()[key]
        raise TypeError("SearchResponse indices must be integers, slices, or field names")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "success": self.ok,
            "results": self.results,
            "count": len(self.results),
            "error": self.error,
        }


@dataclass(frozen=True)
class _MemorySource:
    source_id: str
    vault_id: str
    note_id: str
    path: str
    content_hash: str
    title: str | None
    aliases: tuple[str, ...]
    chunks: tuple[MarkdownChunk, ...]


@dataclass(frozen=True)
class _Snippet:
    source: _MemorySource
    chunk: MarkdownChunk
    match_kind: str
    rank_group: int
    fts_rank: float = 0.0


def _source_id(vault_id: str, note_id: str) -> str:
    return hashlib.sha256(f"{vault_id}\0{note_id}".encode("utf-8")).hexdigest()


def _literal_fts_query(query: str) -> str:
    """Make a single FTS5 phrase; never pass caller text as an expression."""

    return '"' + query.replace('"', '""') + '"'


def _like_pattern(query: str) -> str:
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _as_aliases(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if item is not None)


def _project_matches(path: str, project: str | None) -> bool:
    if project is None or not project.strip():
        return True
    raw = project.strip().replace("\\", "/").strip("/")
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        return False
    prefix = raw if raw == "10-Projects" or raw.startswith("10-Projects/") else f"10-Projects/{raw}"
    return path == prefix or path.startswith(prefix + "/")


def _merge_chunks(chunks: Iterable[tuple[MarkdownChunk, float]]) -> list[tuple[MarkdownChunk, float]]:
    """Merge chunks whose source ranges overlap/touch, retaining best rank."""

    ordered = sorted(chunks, key=lambda item: (item[0].line_start, item[0].line_end, item[0].ordinal))
    merged: list[tuple[MarkdownChunk, float]] = []
    for chunk, rank in ordered:
        if not merged:
            merged.append((chunk, rank))
            continue
        previous, previous_rank = merged[-1]
        if chunk.line_start <= previous.line_end + 1:
            if previous.line_end == chunk.line_end and previous.content and chunk.content:
                separator = "" if previous.content.endswith("\n") else ""
            else:
                separator = "" if previous.content.endswith("\n") or chunk.content.startswith("\n") else "\n"
            merged_chunk = MarkdownChunk(
                ordinal=previous.ordinal,
                kind=previous.kind,
                content=previous.content + separator + chunk.content,
                heading=previous.heading or chunk.heading,
                line_start=min(previous.line_start, chunk.line_start),
                line_end=max(previous.line_end, chunk.line_end),
            )
            merged[-1] = (merged_chunk, min(previous_rank, rank))
        else:
            merged.append((chunk, rank))
    return merged


def _snippet_dict(snippet: _Snippet) -> dict[str, Any]:
    source = snippet.source
    ref = SourceRef(
        source_id=source.source_id,
        vault_id=source.vault_id,
        note_id=source.note_id,
        path=source.path,
        heading=snippet.chunk.heading,
        line_start=snippet.chunk.line_start,
        line_end=snippet.chunk.line_end,
        content_hash=source.content_hash,
    )
    provenance = ref.to_dict()
    return {
        "source_ref": provenance,
        "provenance": provenance,
        "source_id": source.source_id,
        "vault_id": source.vault_id,
        "note_id": source.note_id,
        "path": source.path,
        "heading": snippet.chunk.heading,
        "line_start": snippet.chunk.line_start,
        "line_end": snippet.chunk.line_end,
        "content_hash": source.content_hash,
        "hash": source.content_hash,
        "content": snippet.chunk.content,
        "snippet": snippet.chunk.content,
        "match_kind": snippet.match_kind,
        "fts_rank": snippet.fts_rank,
        "source_content_untrusted": True,
        "untrusted_data": True,
    }


class KnowledgeRetriever:
    """Search work SQLite and explicitly granted personal sources.

    Personal sources are parsed and matched only in this process.  They are
    never written to ``KnowledgeIndex`` or any other persistent store.
    """

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        work_vault: VaultRegistration | None = None,
        personal_vault: VaultRegistration | None = None,
        grant_manager: TaskGrantManager | None = None,
    ):
        self.index = index
        self.work_vault = work_vault
        self.personal_vault = personal_vault
        self.grant_manager = grant_manager

    def _work_sources(self) -> tuple[list[_MemorySource], dict[str, list[MarkdownChunk]]]:
        if not self.index._database_healthy():
            raise IndexUnavailableError("Derived knowledge index is unavailable or stale")
        connection = None
        try:
            connection = self.index._connect()
            rows = connection.execute(
                """
                SELECT source_id, vault_id, note_id, path, content_hash, title, aliases_json
                FROM sources WHERE vault_id = ?
                """,
                (self.work_vault.vault_id if self.work_vault else "work",),
            ).fetchall()
            chunk_rows = connection.execute(
                """
                SELECT chunk_id, source_id, ordinal, kind, heading, line_start, line_end, content
                FROM chunks ORDER BY source_id, ordinal
                """
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise IndexUnavailableError(f"Unable to query derived knowledge index: {error}") from error
        finally:
            if connection is not None:
                connection.close()

        chunks_by_source: dict[str, list[MarkdownChunk]] = {}
        for row in chunk_rows:
            chunks_by_source.setdefault(str(row["source_id"]), []).append(
                MarkdownChunk(
                    ordinal=int(row["ordinal"]),
                    kind=str(row["kind"]),
                    content=str(row["content"]),
                    heading=str(row["heading"]) if row["heading"] is not None else None,
                    line_start=int(row["line_start"]),
                    line_end=int(row["line_end"]),
                )
            )
        sources = [
            _MemorySource(
                source_id=str(row["source_id"]),
                vault_id=str(row["vault_id"]),
                note_id=str(row["note_id"]),
                path=str(row["path"]),
                content_hash=str(row["content_hash"]),
                title=str(row["title"]) if row["title"] is not None else None,
                aliases=_as_aliases(json.loads(row["aliases_json"])),
                chunks=tuple(chunks_by_source.get(str(row["source_id"]), [])),
            )
            for row in rows
        ]
        return sources, chunks_by_source

    def _fts_matches(
        self,
        query: str,
        *,
        source_ids: set[str] | None = None,
    ) -> dict[str, list[tuple[MarkdownChunk, float]]]:
        if not self.index._database_healthy():
            raise IndexUnavailableError("Derived knowledge index is unavailable or stale")
        connection = None
        try:
            connection = self.index._connect()
            if len(query) < 3:
                pattern = _like_pattern(query)
                rows = connection.execute(
                    """
                    SELECT c.chunk_id, c.source_id, c.ordinal, c.kind, c.heading,
                           c.line_start, c.line_end, c.content
                    FROM chunks AS c
                    WHERE c.content LIKE ? ESCAPE '\\'
                       OR COALESCE(c.heading, '') LIKE ? ESCAPE '\\'
                    """,
                    (pattern, pattern),
                ).fetchall()
                ranks = {str(row["chunk_id"]): 0.0 for row in rows}
            else:
                rows = connection.execute(
                    """
                    SELECT c.chunk_id, c.source_id, c.ordinal, c.kind, c.heading,
                           c.line_start, c.line_end, c.content,
                           bm25(chunks_fts) AS fts_rank
                    FROM chunks_fts
                    JOIN chunks AS c ON c.chunk_id = chunks_fts.chunk_id
                    WHERE chunks_fts MATCH ?
                    """,
                    (_literal_fts_query(query),),
                ).fetchall()
                ranks = {str(row["chunk_id"]): float(row["fts_rank"]) for row in rows}
        except sqlite3.DatabaseError as error:
            raise IndexUnavailableError(f"Unable to query full-text index: {error}") from error
        finally:
            if connection is not None:
                connection.close()

        matches: dict[str, list[tuple[MarkdownChunk, float]]] = {}
        for row in rows:
            source_id = str(row["source_id"])
            if source_ids is not None and source_id not in source_ids:
                continue
            chunk = MarkdownChunk(
                ordinal=int(row["ordinal"]),
                kind=str(row["kind"]),
                content=str(row["content"]),
                heading=str(row["heading"]) if row["heading"] is not None else None,
                line_start=int(row["line_start"]),
                line_end=int(row["line_end"]),
            )
            matches.setdefault(source_id, []).append((chunk, ranks[str(row["chunk_id"])]))
        return matches

    @staticmethod
    def _rank_sources(
        query: str,
        sources: Iterable[_MemorySource],
        body_matches: Mapping[str, list[tuple[MarkdownChunk, float]]],
        *,
        project: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query_folded = query.casefold()
        candidates: list[_Snippet] = []
        for source in sources:
            if not _project_matches(source.path, project):
                continue
            title = source.title or (source.chunks[0].heading if source.chunks else None)
            title_folded = title.casefold() if title else ""
            if title_folded == query_folded and title:
                rank_group, match_kind = 0, "title_exact"
            elif query_folded in title_folded and title:
                rank_group, match_kind = 1, "title_contains"
            elif any(query_folded in alias.casefold() for alias in source.aliases):
                rank_group, match_kind = 2, "alias"
            elif source.source_id in body_matches:
                rank_group, match_kind = 3, "body"
            else:
                continue

            body = body_matches.get(source.source_id, [])
            selected = body if body else ([(source.chunks[0], 0.0)] if source.chunks else [])
            for chunk, fts_rank in _merge_chunks(selected):
                candidates.append(_Snippet(source, chunk, match_kind, rank_group, fts_rank))

        candidates.sort(
            key=lambda item: (
                item.rank_group,
                item.fts_rank if item.rank_group == 3 else 0.0,
                item.source.path,
                item.chunk.line_start,
                item.chunk.line_end,
                item.source.source_id,
            )
        )
        return [_snippet_dict(item) for item in candidates[:limit]]

    def _personal_sources(self, task_id: str) -> list[_MemorySource]:
        if self.personal_vault is None or self.grant_manager is None:
            raise AccessDeniedError("Personal search requires an in-process TaskGrantManager grant")
        if not task_id or not isinstance(task_id, str):
            raise AccessDeniedError("Personal search requires a task binding")
        grants = getattr(self.grant_manager, "_grants", {})
        grant = grants.get((task_id, self.personal_vault.vault_id))
        if grant is None:
            raise AccessDeniedError("Task has no active personal-vault grant")
        # check_access performs expiry and canonical-root checks before paths are read.
        granted_paths = tuple(sorted(grant.granted_paths))
        if not any(self.grant_manager.check_access(task_id, path, self.personal_vault) for path in granted_paths):
            raise AccessDeniedError("Task has no active personal-vault grant")

        files: list[Path] = []
        for relative in granted_paths:
            if not self.grant_manager.check_access(task_id, relative, self.personal_vault):
                continue
            valid, _, resolved = validate_vault_path(self.personal_vault, relative, require_formal=False)
            if not valid or resolved is None:
                continue
            if resolved.is_file():
                if resolved.suffix.lower() == ".md":
                    files.append(resolved)
                continue
            if not resolved.is_dir():
                continue
            for current_root, dir_names, file_names in __import__("os").walk(
                resolved, topdown=True, followlinks=False
            ):
                current = Path(current_root)
                dir_names[:] = [name for name in dir_names if not (current / name).is_symlink()]
                for name in file_names:
                    candidate = current / name
                    if candidate.suffix.lower() == ".md" and not candidate.is_symlink():
                        relative_candidate = candidate.relative_to(self.personal_vault.path.resolve()).as_posix()
                        if self.grant_manager.check_access(task_id, relative_candidate, self.personal_vault):
                            files.append(candidate)

        sources: list[_MemorySource] = []
        for path in sorted(set(files), key=lambda item: item.as_posix()):
            relative = path.relative_to(self.personal_vault.path.resolve()).as_posix()
            try:
                data = path.read_bytes()
            except OSError:
                continue
            parsed = parse_markdown_bytes(data, path=relative)
            if any(item.code == "invalid_encoding" for item in parsed.diagnostics):
                continue
            note_id = parsed.note_id or relative
            aliases = _as_aliases(parsed.frontmatter.get("aliases", []))
            sources.append(
                _MemorySource(
                    source_id=_source_id(self.personal_vault.vault_id, note_id),
                    vault_id=self.personal_vault.vault_id,
                    note_id=note_id,
                    path=relative,
                    content_hash=hashlib.sha256(data).hexdigest(),
                    title=str(parsed.frontmatter["title"])
                    if parsed.frontmatter.get("title") is not None
                    else None,
                    aliases=aliases,
                    chunks=tuple(chunk_markdown(parsed, max_chars=self.index.max_chunk_chars)),
                )
            )
        return sources

    def _search_personal(
        self,
        query: str,
        *,
        task_id: str | None,
        project: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not task_id:
            raise AccessDeniedError("Personal search requires a task binding")
        sources = self._personal_sources(task_id)
        query_folded = query.casefold()
        body_matches: dict[str, list[tuple[MarkdownChunk, float]]] = {}
        for source in sources:
            matched = [
                (chunk, 0.0)
                for chunk in source.chunks
                if query_folded in chunk.content.casefold() or query_folded in (chunk.heading or "").casefold()
            ]
            if matched:
                body_matches[source.source_id] = matched
        return self._rank_sources(query, sources, body_matches, project=project, limit=limit)

    def search(
        self,
        query: str,
        *,
        task_id: str | None = None,
        project: str | None = None,
        limit: int = 8,
        vault_id: str = "work",
        include_related: bool = False,
    ) -> SearchResponse:
        """Search safely and deterministically; source text remains untrusted data."""

        if not isinstance(query, str):
            raise TypeError("query must be text")
        query = query.strip().replace("\x00", "")
        if not query or limit <= 0:
            return SearchResponse("no_results")
        try:
            if vault_id == "personal":
                results = self._search_personal(
                    query, task_id=task_id, project=project, limit=min(limit, 8)
                )
            elif vault_id == (self.work_vault.vault_id if self.work_vault else "work") or vault_id == "work":
                sources, _ = self._work_sources()
                source_ids = {source.source_id for source in sources}
                body_matches = self._fts_matches(query, source_ids=source_ids)
                results = self._rank_sources(
                    query, sources, body_matches, project=project, limit=min(limit, 8)
                )
            else:
                raise AccessDeniedError("Requested vault is not registered")
        except AccessDeniedError as error:
            return SearchResponse("access_denied", error={"code": "access_denied", "message": str(error)})
        except IndexUnavailableError as error:
            return SearchResponse("index_error", error={"code": "index_unavailable", "message": str(error)})
        if not results:
            return SearchResponse("no_results")
        response = SearchResponse("ok", results=results)
        if include_related:
            for result in response.results:
                result["related_notes"] = self.related_notes(result["source_ref"], task_id=task_id)
        return response

    def search_or_raise(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        response = self.search(query, **kwargs)
        if response.status == "index_error":
            raise IndexUnavailableError(response.error["message"] if response.error else "index unavailable")
        if response.status == "access_denied":
            raise AccessDeniedError(response.error["message"] if response.error else "access denied")
        return response.results

    search_response = search

    def related_notes(
        self,
        source: SourceRef | Mapping[str, Any],
        *,
        task_id: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return only direct, explicitly stored wiki-link targets; never recurse."""

        ref = SourceRef.from_mapping(source)
        if ref.vault_id == "personal":
            # Personal source links are not persisted or expanded by default.
            return []
        if not self.index._database_healthy():
            raise IndexUnavailableError("Derived knowledge index is unavailable or stale")
        connection = None
        try:
            connection = self.index._connect()
            links = connection.execute(
                "SELECT target FROM links WHERE source_id = ? ORDER BY target LIMIT ?",
                (ref.source_id, limit),
            ).fetchall()
            targets = [str(row[0]) for row in links]
            results: list[dict[str, Any]] = []
            for target in targets:
                row = connection.execute(
                    """
                    SELECT source_id, vault_id, note_id, path, content_hash
                    FROM sources
                    WHERE vault_id = ? AND (note_id = ? OR path = ? OR path = ? OR path = ?)
                    ORDER BY path LIMIT 1
                    """,
                    (ref.vault_id, target, target, target + ".md", target.removesuffix(".md")),
                ).fetchone()
                if row is None:
                    continue
                related = SourceRef(
                    source_id=str(row["source_id"]),
                    vault_id=str(row["vault_id"]),
                    note_id=str(row["note_id"]),
                    path=str(row["path"]),
                    heading=None,
                    line_start=1,
                    line_end=1,
                    content_hash=str(row["content_hash"]),
                ).to_dict()
                results.append(related)
            return results
        except sqlite3.DatabaseError as error:
            raise IndexUnavailableError(f"Unable to query related notes: {error}") from error
        finally:
            if connection is not None:
                connection.close()


__all__ = [
    "IndexUnavailableError",
    "InvalidQueryError",
    "KnowledgeRetriever",
    "RetrievalError",
    "SearchResponse",
]
