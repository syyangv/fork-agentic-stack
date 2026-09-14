import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".agent" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


class TestKnowledgeIndex(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.vault = self.root / "vault"
        self.db = self.root / "runtime" / "index.sqlite3"
        self.vault.mkdir()
        for directory in ("00-Inbox", "10-Projects", "20-Research", "30-Decisions", "40-Lessons", "90-Archive"):
            (self.vault / directory).mkdir()

        from knowledge_core.config import FORMAL_DIR_ALLOWLIST, VaultRegistration

        self.registration = VaultRegistration(
            vault_id="work",
            path=self.vault,
            is_personal=False,
            indexed=True,
            allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_note(self, relative, content, *, raw=False):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw:
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def query(self, sql, parameters=()):
        with sqlite3.connect(self.db) as connection:
            return connection.execute(sql, parameters).fetchall()

    def test_rebuild_incremental_move_delete_and_immutability(self):
        from knowledge_core.index import KnowledgeIndex

        original = "---\nid: k-move-001\n---\n# Durable\n\nunique blue kiwi\n"
        source = self.write_note("10-Projects/movable.md", original)
        before = source.read_bytes()
        index = KnowledgeIndex(self.db)

        first = index.sync(self.registration)
        self.assertEqual(first.added, 1)
        self.assertEqual(index.search("blue kiwi")[0]["path"], "10-Projects/movable.md")
        self.assertEqual(source.read_bytes(), before)

        moved = self.vault / "20-Research/moved.md"
        source.rename(moved)
        move_result = index.sync(self.registration)
        self.assertEqual(move_result.moved, 1)
        self.assertEqual(move_result.added, 0)
        self.assertEqual(index.search("blue kiwi")[0]["path"], "20-Research/moved.md")

        moved.unlink()
        delete_result = index.sync(self.registration)
        self.assertEqual(delete_result.deleted, 1)
        self.assertEqual(index.search("blue kiwi"), [])
        self.assertEqual(self.query("SELECT COUNT(*) FROM sources")[0][0], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM chunks")[0][0], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM links")[0][0], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM chunks_fts")[0][0], 0)

    def test_path_identified_move_is_delete_and_add(self):
        from knowledge_core.index import KnowledgeIndex

        source = self.write_note("10-Projects/path-note.md", "# Path note\n\npath identity\n")
        index = KnowledgeIndex(self.db)
        index.sync(self.registration)
        source.rename(self.vault / "10-Projects/path-note-moved.md")

        result = index.sync(self.registration)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.deleted, 1)
        self.assertEqual(result.moved, 0)
        self.assertEqual(index.search("path identity")[0]["path"], "10-Projects/path-note-moved.md")

    def test_scope_symlinks_invalid_encoding_duplicates_and_out_of_scope(self):
        from knowledge_core.index import DuplicateNoteIDError, KnowledgeIndex

        self.write_note("10-Projects/good.md", "# Good\n\nvisible needle\n")
        self.write_note("00-Inbox/inbox.md", "inbox ghost needle")
        self.write_note("90-Archive/archive.md", "archive ghost needle")
        self.write_note("10-Projects/config/settings.md", "config ghost needle")
        self.write_note("10-Projects/attachments/file.md", "attachment ghost needle")
        external = self.root / "external.md"
        external.write_text("external ghost needle", encoding="utf-8")
        self.write_note("10-Projects/symlink.md", "unused")
        (self.vault / "10-Projects/symlink.md").unlink()
        (self.vault / "10-Projects/symlink.md").symlink_to(external)
        self.write_note("10-Projects/bad.md", b"\xff\xfe\x00", raw=True)

        index = KnowledgeIndex(self.db)
        result = index.sync(self.registration)
        self.assertEqual(result.added, 1)  # only good; config/attachment are out-of-scope by policy
        codes = {diagnostic.code for diagnostic in result.diagnostics}
        self.assertIn("symlink_skipped", codes)
        self.assertIn("invalid_encoding", codes)
        results = index.search("needle")
        indexed_paths = {row["path"] for row in results}
        self.assertEqual(indexed_paths, {"10-Projects/good.md"})
        self.assertNotIn("00-Inbox/inbox.md", indexed_paths)
        self.assertNotIn("90-Archive/archive.md", indexed_paths)
        self.assertNotIn("external.md", indexed_paths)

        self.write_note("20-Research/duplicate.md", "---\nid: k-duplicate\n---\nDuplicate\n")
        self.write_note("30-Decisions/duplicate-too.md", "---\nid: k-duplicate\n---\nDuplicate too\n")
        with self.assertRaises(DuplicateNoteIDError):
            index.sync(self.registration)
        self.assertEqual(index.search("visible needle")[0]["path"], "10-Projects/good.md")

    def test_corrupt_database_is_rebuilt_by_safe_replacement(self):
        from knowledge_core.index import KnowledgeIndex

        self.write_note("10-Projects/rebuild.md", "# Rebuilt\n\nreplacement marker\n")
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b"not a sqlite database")
        index = KnowledgeIndex(self.db)
        result = index.sync(self.registration)
        self.assertEqual(result.action, "rebuild")
        self.assertEqual(index.search("replacement marker")[0]["path"], "10-Projects/rebuild.md")
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_duplicate_error_does_not_change_existing_database(self):
        from knowledge_core.index import DuplicateNoteIDError, KnowledgeIndex

        source = self.write_note("10-Projects/stable.md", "---\nid: k-stable\n---\n\nstable marker\n")
        index = KnowledgeIndex(self.db)
        index.sync(self.registration)
        database_before = self.db.read_bytes()
        self.write_note("20-Research/dupe.md", "---\nid: k-stable\n---\n\nconflicting marker\n")
        with self.assertRaises(DuplicateNoteIDError):
            index.sync(self.registration)
        self.assertEqual(index.search("stable marker")[0]["path"], "10-Projects/stable.md")
        self.assertNotEqual(database_before, b"")
        self.assertEqual(source.read_text(encoding="utf-8").count("stable marker"), 1)

    def test_path_scoped_refresh_updates_only_the_affected_formal_path(self):
        from knowledge_core.index import KnowledgeIndex
        from unittest.mock import patch

        baseline = self.write_note("10-Projects/baseline.md", "baseline marker\n")
        index = KnowledgeIndex(self.db)
        index.sync(self.registration)

        affected = self.write_note("20-Research/affected.md", "path-only marker\n")
        with patch.object(index, "_scan", side_effect=AssertionError("path refresh must not scan the vault")):
            result = index.sync_path(self.registration, "20-Research/affected.md")
        self.assertEqual(result.action, "path_incremental")
        self.assertEqual(result.added, 1)
        self.assertEqual(index.search("path-only marker")[0]["path"], "20-Research/affected.md")
        self.assertEqual(index.search("baseline marker")[0]["path"], "10-Projects/baseline.md")

        affected.unlink()
        with patch.object(index, "_scan", side_effect=AssertionError("path refresh must not scan the vault")):
            deleted = index.sync_path(self.registration, "20-Research/affected.md")
        self.assertEqual(deleted.deleted, 1)
        self.assertEqual(index.search("path-only marker"), [])
        self.assertTrue(baseline.exists())


if __name__ == "__main__":
    unittest.main()
