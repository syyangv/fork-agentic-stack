import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".agent" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from knowledge_core.config import FORMAL_DIR_ALLOWLIST, VaultRegistration  # noqa: E402
from knowledge_core.grants import TaskGrantManager  # noqa: E402
from knowledge_core.index import KnowledgeIndex  # noqa: E402
from knowledge_core.provenance import (  # noqa: E402
    AccessDeniedError,
    SourceChangedError,
    SourceReader,
)
from knowledge_core.retrieval import KnowledgeRetriever  # noqa: E402


class TestKnowledgeRetrieval(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.work = self.root / "work"
        self.personal = self.root / "personal"
        for vault in (self.work, self.personal):
            vault.mkdir()
        for directory in FORMAL_DIR_ALLOWLIST:
            (self.work / directory).mkdir()
        (self.personal / "Journal").mkdir()
        self.work_registration = VaultRegistration(
            vault_id="work",
            path=self.work,
            is_personal=False,
            indexed=True,
            allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
        )
        self.personal_registration = VaultRegistration(
            vault_id="personal",
            path=self.personal,
            is_personal=True,
            indexed=False,
        )
        self.db = self.root / "runtime" / "index.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_work(self, relative, content):
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_personal(self, relative, content):
        path = self.personal / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def make_retriever(self, *, max_chunk_chars=2_000, grants=None):
        index = KnowledgeIndex(self.db, max_chunk_chars=max_chunk_chars)
        index.sync(self.work_registration)
        return (
            index,
            KnowledgeRetriever(
                index,
                work_vault=self.work_registration,
                personal_vault=self.personal_registration,
                grant_manager=grants,
            ),
        )

    def test_title_alias_body_order_and_explicit_project_filter(self):
        self.write_work(
            "10-Projects/alpha/exact.md",
            "---\ntitle: Deploy\n---\n\nexact body\n",
        )
        self.write_work(
            "10-Projects/alpha/contains.md",
            "---\ntitle: Deploy Checklist\n---\n\ncontains body\n",
        )
        self.write_work(
            "10-Projects/beta/alias.md",
            "---\ntitle: Release\naliases: [Deploy]\n---\n\nalias body\n",
        )
        self.write_work(
            "20-Research/body.md",
            "---\ntitle: Research\n---\n\nDeploy in body\n",
        )
        _, retriever = self.make_retriever()

        response = retriever.search("Deploy", limit=8)
        self.assertEqual(response.status, "ok")
        self.assertEqual(
            [item["match_kind"] for item in response.results[:4]],
            ["title_exact", "title_contains", "alias", "body"],
        )
        self.assertEqual(response.results[0]["path"], "10-Projects/alpha/exact.md")

        all_paths = {item["path"] for item in response.results}
        self.assertIn("10-Projects/beta/alias.md", all_paths)
        alpha = retriever.search("Deploy", project="alpha")
        self.assertEqual(
            {item["path"] for item in alpha.results},
            {"10-Projects/alpha/exact.md", "10-Projects/alpha/contains.md"},
        )

    def test_safe_short_and_fts_queries_do_not_execute_expressions(self):
        self.write_work(
            "10-Projects/safe.md",
            "# Safe\n\nabc OR def\n引号测试\na_b percent%\n",
        )
        _, retriever = self.make_retriever()

        self.assertEqual(retriever.search('abc" OR *').status, "no_results")
        self.assertEqual(retriever.search("引号测试").results[0]["path"], "10-Projects/safe.md")
        self.assertEqual(retriever.search("a_b").results[0]["path"], "10-Projects/safe.md")
        self.assertEqual(retriever.search("percent%").results[0]["path"], "10-Projects/safe.md")

    def test_results_merge_overlapping_chunks_and_include_complete_provenance(self):
        repeated = "needle " * 30
        self.write_work("10-Projects/long.md", f"# Long\n\n{repeated}\n")
        _, retriever = self.make_retriever(max_chunk_chars=40)

        response = retriever.search("needle", limit=20)
        long_results = [item for item in response.results if item["path"] == "10-Projects/long.md"]
        self.assertEqual(len(long_results), 1)
        item = long_results[0]
        for key in ("vault_id", "note_id", "path", "heading", "line_start", "line_end", "content_hash"):
            self.assertIn(key, item)
        self.assertEqual(item["source_ref"]["content_hash"], item["content_hash"])
        self.assertLessEqual(item["line_start"], item["line_end"])

    def test_no_results_is_distinct_from_index_failure(self):
        self.write_work("10-Projects/known.md", "known marker\n")
        index, retriever = self.make_retriever()
        self.assertEqual(retriever.search("not present").status, "no_results")

        broken = KnowledgeRetriever(
            KnowledgeIndex(self.root / "missing" / "not-an-index.sqlite3"),
            work_vault=self.work_registration,
        )
        response = broken.search("known")
        self.assertEqual(response.status, "index_error")
        self.assertEqual(response.results, [])
        self.assertNotEqual(index.search("known"), [])

    def test_source_reader_rejects_stale_work_source(self):
        path = self.write_work("10-Projects/source.md", "# Source\n\nold content\n")
        index, retriever = self.make_retriever()
        result = retriever.search("old content").results[0]
        reader = SourceReader(index, work_vault=self.work_registration)
        read = reader.read_source(result["source_ref"], task_id="task-1")
        self.assertIn("old content", read.content)

        path.write_text("# Source\n\nnew content\n", encoding="utf-8")
        with self.assertRaises(SourceChangedError):
            reader.read_source(result["source_ref"], task_id="task-1")

    def test_personal_search_and_read_require_live_task_grant(self):
        self.write_personal("Journal/private.md", "private marker\n")
        grants = TaskGrantManager()
        index, retriever = self.make_retriever(grants=grants)
        denied = retriever.search("private marker", vault_id="personal", task_id="task-a")
        self.assertEqual(denied.status, "access_denied")
        self.assertEqual(denied.results, [])

        grants.grant("task-a", "Journal/private.md", self.personal_registration)
        allowed = retriever.search("private marker", vault_id="personal", task_id="task-a")
        self.assertEqual(allowed.status, "ok")
        result = allowed.results[0]
        reader = SourceReader(
            index,
            work_vault=self.work_registration,
            personal_vault=self.personal_registration,
            grant_manager=grants,
        )
        self.assertIn("private marker", reader.read_source(result["source_ref"], task_id="task-a").content)
        with self.assertRaises(AccessDeniedError):
            reader.read_source(result["source_ref"], task_id="task-b")

        restarted = TaskGrantManager()
        restarted_reader = SourceReader(
            index,
            work_vault=self.work_registration,
            personal_vault=self.personal_registration,
            grant_manager=restarted,
        )
        with self.assertRaises(AccessDeniedError):
            restarted_reader.read_source(result["source_ref"], task_id="task-a")
        self.assertFalse((self.db).exists() and "private marker" in self.db.read_bytes().decode("utf-8", "ignore"))


if __name__ == "__main__":
    unittest.main()
