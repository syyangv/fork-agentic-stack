"""Rebuildable SQLite/FTS5 index for the formal Agent Knowledge Vault."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from time import time
from typing import Any, Iterable
from urllib.parse import quote

from .chunks import DEFAULT_MAX_CHARS, MarkdownChunk, chunk_markdown
from .config import FORMAL_DIR_ALLOWLIST, VaultRegistration
from .id_manager import identify_note
from .parser import ParseDiagnostic, ParsedMarkdown, parse_markdown_bytes
from .scope import validate_vault_path


SCHEMA_VERSION = "2"
EXCLUDED_DIRECTORY_NAMES = {".obsidian", "config", "configs", "attachments", "_attachments"}


@dataclass(frozen=True)
class IndexDiagnostic:
    code: str
    message: str
    severity: str = "warning"
    path: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message, "severity": self.severity}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass
class IndexRun:
    action: str
    added: int = 0
    modified: int = 0
    deleted: int = 0
    moved: int = 0
    unchanged: int = 0
    skipped: int = 0
    diagnostics: list[IndexDiagnostic] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "added": self.added,
            "modified": self.modified,
            "deleted": self.deleted,
            "moved": self.moved,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class _SourceRecord:
    source_id: str
    vault_id: str
    note_id: str
    path: str
    content_hash: str
    byte_size: int
    mtime_ns: int
    title: str | None
    aliases: list[str]
    parsed: ParsedMarkdown
    chunks: list[MarkdownChunk]


class KnowledgeIndexError(RuntimeError):
    """Base error for index operations that must not partially apply."""


class DuplicateNoteIDError(KnowledgeIndexError):
    def __init__(self, note_id: str, paths: Iterable[str]):
        self.note_id = note_id
        self.paths = tuple(paths)
        joined = ", ".join(self.paths)
        super().__init__(f"Duplicate note ID '{note_id}' found in: {joined}")


class KnowledgeIndex:
    """Own only derived state; never writes to a vault source file."""

    def __init__(self, db_path: str | Path, *, max_chunk_chars: int = DEFAULT_MAX_CHARS):
        if max_chunk_chars <= 0:
            raise ValueError("max_chunk_chars must be positive")
        self.db_path = Path(db_path).expanduser()
        self.max_chunk_chars = max_chunk_chars

    def _database_healthy(self) -> bool:
        if not self.db_path.is_file():
            return False
        connection: sqlite3.Connection | None = None
        try:
            uri = f"file:{quote(str(self.db_path.resolve()), safe='/')}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                return False
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            required = {"schema_meta", "sources", "chunks", "links", "chunks_fts"}
            if not required.issubset(tables):
                return False
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?", ("schema_version",)
            ).fetchone()
            return bool(version and version[0] == SCHEMA_VERSION)
        except sqlite3.DatabaseError:
            return False
        finally:
            if connection is not None:
                connection.close()

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        target = path or self.db_path
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                vault_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                title TEXT,
                aliases_json TEXT NOT NULL,
                indexed_at REAL NOT NULL,
                UNIQUE(vault_id, note_id),
                UNIQUE(vault_id, path)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL,
                heading TEXT,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(source_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS links (
                link_id INTEGER PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                target TEXT NOT NULL,
                alias TEXT,
                raw TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                UNIQUE(source_id, target, alias, raw, line_start, line_end)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                content,
                heading,
                tokenize='trigram'
            );
            INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '2');
            """
        )

    @staticmethod
    def _source_id(vault_id: str, note_id: str) -> str:
        value = f"{vault_id}\0{note_id}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _diagnostic_from_parse(diagnostic: ParseDiagnostic) -> IndexDiagnostic:
        return IndexDiagnostic(
            code=diagnostic.code,
            message=diagnostic.message,
            severity=diagnostic.severity,
            path=diagnostic.path,
        )

    def _scan(self, vault: VaultRegistration) -> tuple[list[_SourceRecord], list[IndexDiagnostic], int]:
        if vault.is_personal or not vault.indexed:
            raise KnowledgeIndexError("Only an indexed non-personal vault may be indexed")
        root = vault.path.resolve()
        if not root.is_dir():
            raise KnowledgeIndexError(f"Vault does not exist: {root}")

        allowed = [directory for directory in (vault.allowed_dirs or list(FORMAL_DIR_ALLOWLIST)) if directory in FORMAL_DIR_ALLOWLIST]
        diagnostics: list[IndexDiagnostic] = []
        records: list[_SourceRecord] = []
        seen_note_ids: dict[str, list[str]] = {}
        skipped = 0

        for directory in allowed:
            directory_path = root / directory
            if not directory_path.is_dir():
                continue
            for current_root, dir_names, file_names in os.walk(directory_path, topdown=True, followlinks=False):
                current = Path(current_root)
                kept_dirs: list[str] = []
                for name in dir_names:
                    candidate = current / name
                    if candidate.is_symlink():
                        skipped += 1
                        diagnostics.append(
                            IndexDiagnostic(
                                "symlink_skipped",
                                "Symlink directory is never indexed.",
                                path=candidate.relative_to(root).as_posix(),
                            )
                        )
                    elif name in EXCLUDED_DIRECTORY_NAMES:
                        skipped += 1
                        diagnostics.append(
                            IndexDiagnostic(
                                "out_of_scope_skipped",
                                "Configuration or attachment directory is outside the formal index scope.",
                                path=candidate.relative_to(root).as_posix(),
                            )
                        )
                    else:
                        kept_dirs.append(name)
                dir_names[:] = kept_dirs

                for name in file_names:
                    if not name.lower().endswith(".md"):
                        continue
                    path = current / name
                    relative = path.relative_to(root).as_posix()
                    if path.is_symlink():
                        skipped += 1
                        diagnostics.append(IndexDiagnostic("symlink_skipped", "Symlink file is never indexed.", path=relative))
                        continue
                    valid, message, _ = validate_vault_path(vault, relative, require_formal=True)
                    if not valid:
                        skipped += 1
                        diagnostics.append(IndexDiagnostic("unsafe_path", message, severity="error", path=relative))
                        continue
                    try:
                        data = path.read_bytes()
                    except OSError as error:
                        skipped += 1
                        diagnostics.append(IndexDiagnostic("unreadable_source", str(error), severity="error", path=relative))
                        continue
                    parsed = parse_markdown_bytes(data, path=relative)
                    diagnostics.extend(self._diagnostic_from_parse(item) for item in parsed.diagnostics)
                    if any(item.code == "invalid_encoding" for item in parsed.diagnostics):
                        skipped += 1
                        continue
                    note_id = parsed.note_id or identify_note(relative, parsed.content)
                    seen_note_ids.setdefault(note_id, []).append(relative)
                    stat = path.stat()
                    aliases = parsed.frontmatter.get("aliases", [])
                    if isinstance(aliases, str):
                        aliases = [aliases]
                    if not isinstance(aliases, list):
                        aliases = []
                    records.append(
                        _SourceRecord(
                            source_id=self._source_id(vault.vault_id, note_id),
                            vault_id=vault.vault_id,
                            note_id=note_id,
                            path=relative,
                            content_hash=hashlib.sha256(data).hexdigest(),
                            byte_size=len(data),
                            mtime_ns=stat.st_mtime_ns,
                            title=str(parsed.frontmatter["title"]) if parsed.frontmatter.get("title") is not None else None,
                            aliases=[str(alias) for alias in aliases],
                            parsed=parsed,
                            chunks=chunk_markdown(parsed, max_chars=self.max_chunk_chars),
                        )
                    )

        duplicates = [(note_id, paths) for note_id, paths in seen_note_ids.items() if len(paths) > 1]
        if duplicates:
            note_id, paths = sorted(duplicates, key=lambda item: item[0])[0]
            raise DuplicateNoteIDError(note_id, sorted(paths))
        records.sort(key=lambda item: item.path)
        return records, diagnostics, skipped

    def _scan_path(
        self, vault: VaultRegistration, relative_path: str | Path
    ) -> tuple[_SourceRecord | None, list[IndexDiagnostic], int]:
        """Scan exactly one formal Markdown path for a post-write refresh."""

        if vault.is_personal or not vault.indexed:
            raise KnowledgeIndexError("Only an indexed non-personal vault may be indexed")
        root = vault.path.resolve()
        if not root.is_dir():
            raise KnowledgeIndexError(f"Vault does not exist: {root}")
        if isinstance(relative_path, Path):
            relative_path = relative_path.as_posix()
        if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
            raise KnowledgeIndexError("Path refresh requires a clean relative POSIX path")
        relative = Path(relative_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or relative.as_posix() != relative_path:
            raise KnowledgeIndexError("Path refresh requires a clean relative POSIX path")
        if relative.suffix.lower() != ".md":
            raise KnowledgeIndexError("Path refresh targets Markdown files only")
        diagnostics: list[IndexDiagnostic] = []
        valid, message, _ = validate_vault_path(vault, relative_path, require_formal=True)
        if not valid:
            diagnostics.append(IndexDiagnostic("unsafe_path", message, severity="error", path=relative_path))
            return None, diagnostics, 1

        path = root.joinpath(*relative.parts)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                diagnostics.append(IndexDiagnostic("symlink_skipped", "Symlink path is never indexed.", path=relative_path))
                return None, diagnostics, 1
        if not path.exists():
            return None, diagnostics, 0
        if not path.is_file() or path.suffix.lower() != ".md":
            diagnostics.append(IndexDiagnostic("unsafe_path", "Path refresh targets a Markdown file.", severity="error", path=relative_path))
            return None, diagnostics, 1
        try:
            data = path.read_bytes()
        except OSError as error:
            diagnostics.append(IndexDiagnostic("unreadable_source", str(error), severity="error", path=relative_path))
            return None, diagnostics, 1
        parsed = parse_markdown_bytes(data, path=relative_path)
        diagnostics.extend(self._diagnostic_from_parse(item) for item in parsed.diagnostics)
        if any(item.code == "invalid_encoding" for item in parsed.diagnostics):
            return None, diagnostics, 1
        note_id = parsed.note_id or identify_note(relative_path, parsed.content)
        aliases = parsed.frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, list):
            aliases = []
        stat_result = path.stat()
        return (
            _SourceRecord(
                source_id=self._source_id(vault.vault_id, note_id),
                vault_id=vault.vault_id,
                note_id=note_id,
                path=relative_path,
                content_hash=hashlib.sha256(data).hexdigest(),
                byte_size=len(data),
                mtime_ns=stat_result.st_mtime_ns,
                title=str(parsed.frontmatter["title"]) if parsed.frontmatter.get("title") is not None else None,
                aliases=[str(alias) for alias in aliases],
                parsed=parsed,
                chunks=chunk_markdown(parsed, max_chars=self.max_chunk_chars),
            ),
            diagnostics,
            0,
        )

    def scan_vault(self, vault: VaultRegistration) -> dict[str, Any]:
        """Return a safe inventory useful for diagnostics without opening SQLite."""

        records, diagnostics, skipped = self._scan(vault)
        return {
            "sources": [
                {
                    "source_id": record.source_id,
                    "vault_id": record.vault_id,
                    "note_id": record.note_id,
                    "path": record.path,
                    "content_hash": record.content_hash,
                }
                for record in records
            ],
            "skipped": skipped,
            "diagnostics": [diagnostic.to_dict() for diagnostic in diagnostics],
        }

    @staticmethod
    def _chunk_id(source_id: str, ordinal: int) -> str:
        return hashlib.sha256(f"{source_id}\0{ordinal}".encode("utf-8")).hexdigest()

    def _insert_source(self, connection: sqlite3.Connection, record: _SourceRecord) -> None:
        connection.execute(
            """
            INSERT INTO sources(
                source_id, vault_id, note_id, path, content_hash, byte_size,
                mtime_ns, title, aliases_json, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_id,
                record.vault_id,
                record.note_id,
                record.path,
                record.content_hash,
                record.byte_size,
                record.mtime_ns,
                record.title,
                json.dumps(record.aliases, ensure_ascii=False),
                time(),
            ),
        )
        for chunk in record.chunks:
            chunk_id = self._chunk_id(record.source_id, chunk.ordinal)
            connection.execute(
                """
                INSERT INTO chunks(
                    chunk_id, source_id, ordinal, kind, heading,
                    line_start, line_end, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    record.source_id,
                    chunk.ordinal,
                    chunk.kind,
                    chunk.heading,
                    chunk.line_start,
                    chunk.line_end,
                    chunk.content,
                ),
            )
            connection.execute(
                "INSERT INTO chunks_fts(chunk_id, content, heading) VALUES (?, ?, ?)",
                (chunk_id, chunk.content, chunk.heading),
            )
        for link in record.parsed.wiki_links:
            connection.execute(
                """
                INSERT INTO links(source_id, target, alias, raw, line_start, line_end)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (record.source_id, link.target, link.alias, link.raw, link.line_start, link.line_end),
            )

    @staticmethod
    def _delete_source(connection: sqlite3.Connection, source_id: str) -> None:
        chunk_ids = connection.execute("SELECT chunk_id FROM chunks WHERE source_id = ?", (source_id,)).fetchall()
        for row in chunk_ids:
            connection.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (row[0],))
        connection.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))

    def _replace_source(self, connection: sqlite3.Connection, record: _SourceRecord) -> None:
        self._delete_source(connection, record.source_id)
        self._insert_source(connection, record)

    def _write_full(self, connection: sqlite3.Connection, records: list[_SourceRecord]) -> None:
        self._create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for record in records:
                self._insert_source(connection, record)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _replace_database(self, records: list[_SourceRecord]) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.db_path.name}.", suffix=".rebuild", dir=self.db_path.parent
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        try:
            connection = self._connect(temporary_path)
            try:
                self._write_full(connection, records)
                connection.execute("PRAGMA wal_checkpoint(FULL)")
            finally:
                connection.close()
            with temporary_path.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.db_path)
            try:
                directory_fd = os.open(self.db_path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def sync(self, vault: VaultRegistration, *, rebuild: bool = False) -> IndexRun:
        """Synchronize formal Markdown files, rebuilding safely when needed."""

        records, diagnostics, skipped = self._scan(vault)
        if rebuild or not self._database_healthy():
            self._replace_database(records)
            return IndexRun(
                action="rebuild",
                added=len(records),
                skipped=skipped,
                diagnostics=diagnostics,
            )

        connection: sqlite3.Connection | None = None
        run = IndexRun(action="incremental", skipped=skipped, diagnostics=diagnostics)
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            existing_rows = connection.execute(
                "SELECT source_id, path, content_hash FROM sources WHERE vault_id = ?", (vault.vault_id,)
            ).fetchall()
            existing = {row["source_id"]: row for row in existing_rows}
            current_ids = {record.source_id for record in records}
            for source_id, row in existing.items():
                if source_id not in current_ids:
                    self._delete_source(connection, source_id)
                    run.deleted += 1

            for record in records:
                old = existing.get(record.source_id)
                if old is None:
                    self._insert_source(connection, record)
                    run.added += 1
                elif old["content_hash"] != record.content_hash:
                    self._replace_source(connection, record)
                    run.modified += 1
                    if old["path"] != record.path:
                        run.moved += 1
                elif old["path"] != record.path:
                    connection.execute(
                        "UPDATE sources SET path = ?, mtime_ns = ?, indexed_at = ? WHERE source_id = ?",
                        (record.path, record.mtime_ns, time(), record.source_id),
                    )
                    run.moved += 1
                else:
                    run.unchanged += 1
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
        return run

    def sync_path(self, vault: VaultRegistration, relative_path: str) -> IndexRun:
        """Refresh only one formal path; never scan or rebuild other paths.

        A missing database may be initialized with the affected source only.
        A corrupt existing database is left untouched and requires the caller's
        explicit full ``sync(..., rebuild=True)`` repair operation.
        """

        record, diagnostics, skipped = self._scan_path(vault, relative_path)
        if self.db_path.exists() and not self._database_healthy():
            raise KnowledgeIndexError("Path refresh requires a healthy derived index; run an explicit rebuild")

        database_exists = self.db_path.exists()
        connection: sqlite3.Connection | None = None
        run = IndexRun(action="path_incremental", skipped=skipped, diagnostics=diagnostics)
        try:
            connection = self._connect()
            if not database_exists:
                self._create_schema(connection)
            elif not self._database_healthy():
                raise KnowledgeIndexError("Path refresh requires a healthy derived index; run an explicit rebuild")
            connection.execute("BEGIN IMMEDIATE")
            existing_at_path = connection.execute(
                "SELECT source_id, path, content_hash FROM sources WHERE vault_id = ? AND path = ?",
                (vault.vault_id, relative_path),
            ).fetchone()

            if record is None:
                if existing_at_path is not None:
                    self._delete_source(connection, str(existing_at_path["source_id"]))
                    run.deleted = 1
                connection.commit()
                return run

            existing_by_id = connection.execute(
                "SELECT source_id, path, content_hash FROM sources WHERE vault_id = ? AND source_id = ?",
                (vault.vault_id, record.source_id),
            ).fetchone()
            if existing_by_id is not None and existing_by_id["path"] != relative_path:
                raise DuplicateNoteIDError(record.note_id, [str(existing_by_id["path"]), relative_path])

            if existing_at_path is None:
                self._insert_source(connection, record)
                run.added = 1
            elif existing_at_path["source_id"] != record.source_id:
                self._delete_source(connection, str(existing_at_path["source_id"]))
                self._insert_source(connection, record)
                run.deleted = 1
                run.added = 1
            elif existing_at_path["content_hash"] != record.content_hash:
                self._replace_source(connection, record)
                run.modified = 1
            else:
                run.unchanged = 1
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
        return run

    def rebuild(self, vault: VaultRegistration) -> IndexRun:
        return self.sync(vault, rebuild=True)

    @staticmethod
    def _like_pattern(query: str) -> str:
        return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Small index-level search helper; ranking policy belongs to P3."""

        if not isinstance(query, str):
            raise TypeError("query must be text")
        if limit <= 0 or not query:
            return []
        if not self._database_healthy():
            return []
        connection = self._connect()
        try:
            if len(query) < 3:
                rows = connection.execute(
                    """
                    SELECT s.source_id, s.note_id, s.path, s.content_hash,
                           c.chunk_id, c.kind, c.heading, c.line_start,
                           c.line_end, c.content
                    FROM chunks AS c JOIN sources AS s ON s.source_id = c.source_id
                    WHERE c.content LIKE ? ESCAPE '\\'
                    ORDER BY s.path, c.line_start, c.ordinal LIMIT ?
                    """,
                    (self._like_pattern(query), limit),
                ).fetchall()
            else:
                fts_query = '"' + query.replace('"', '""') + '"'
                rows = connection.execute(
                    """
                    SELECT s.source_id, s.note_id, s.path, s.content_hash,
                           c.chunk_id, c.kind, c.heading, c.line_start,
                           c.line_end, c.content
                    FROM chunks_fts AS f
                    JOIN chunks AS c ON c.chunk_id = f.chunk_id
                    JOIN sources AS s ON s.source_id = c.source_id
                    WHERE chunks_fts MATCH ?
                    ORDER BY bm25(chunks_fts), s.path, c.line_start, c.ordinal LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
