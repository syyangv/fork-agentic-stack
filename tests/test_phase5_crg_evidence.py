import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / ".agent" / "memory"
sys.path.insert(0, str(MEMORY))

from orchestration.providers.crg_evidence import (  # noqa: E402
    CrgEvidenceError,
    CrgEvidenceProvider,
    EvidenceLedger,
)
from orchestration.orchestrator import build_evidence_request  # noqa: E402


REVISION = "a" * 40


class CrgEvidenceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.source = self.repo / "pkg" / "service.py"
        self.source.parent.mkdir()
        self.source.write_text("def handle_order():\n    return True\n")
        self.file_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.data = self.root / "durable" / "repo-graph"
        self.data.mkdir(parents=True)
        self.db = self.data / "graph.db"
        self._build_db(nodes=2)
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({"repos": [{
            "path": str(self.repo), "data_dir": str(self.data), "alias": "repo",
        }]}))
        self.ledger = EvidenceLedger(self.root / "evidence" / "ledger.jsonl")
        self.provider = CrgEvidenceProvider(
            repo_root=self.repo, project_id="1" * 16,
            registry_path=self.registry, ledger=self.ledger,
            revision_resolver=lambda _root: REVISION,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _build_db(self, *, nodes: int, revision: str = REVISION):
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("create table metadata (key text primary key, value text not null)")
            conn.executemany("insert into metadata values (?, ?)", [
                ("schema_version", "9"), ("last_updated", "2026-07-18T04:00:00Z"),
                ("git_head_sha", revision), ("embedding_provider", "none"),
            ])
            conn.execute("""create table nodes (
                id integer primary key, kind text, name text, qualified_name text unique,
                file_path text, line_start integer, line_end integer, file_hash text,
                updated_at real
            )""")
            if nodes:
                conn.execute(
                    "insert into nodes values (1, 'File', 'service.py', 'pkg/service.py', ?, 1, 2, ?, 1)",
                    ("pkg/service.py", self.file_hash),
                )
            if nodes > 1:
                conn.execute(
                    "insert into nodes values (2, 'Function', 'handle_order', ?, ?, 1, 2, ?, 1)",
                    ("pkg/service.py::handle_order", "pkg/service.py", self.file_hash),
                )

    def crg_payload(self, **overrides):
        value = {
            "kind": "crg_node",
            "tool_name": "semantic_search_nodes",
            "repository_root": str(self.repo),
            "repository_revision": REVISION,
            "graph_updated_at": "2026-07-18T04:00:00Z",
            "summary": "handle_order is defined in the order service",
            "confidence_tier": "high",
            "symbols": [{
                "qualified_name": "pkg/service.py::handle_order",
                "file_path": "pkg/service.py",
                "file_hash": "sha256:" + self.file_hash,
            }],
            "relationships": [],
        }
        value.update(overrides)
        return value


class CrgHealthAndRequestTest(CrgEvidenceFixture):
    def test_manifest_declares_phase5_evidence_features(self):
        manifest = json.loads((ROOT / ".agent" / "infrastructure.json").read_text())
        self.assertEqual(manifest["orchestration_phase"], 5)
        self.assertTrue({
            "crg_evidence_requests", "revision_bound_evidence",
            "bounded_evidence_ledger", "explicit_test_run_evidence",
        }.issubset(manifest["features"]))

    def test_healthy_registry_reports_graph_metadata_and_durable_path(self):
        health = self.provider.health()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["nodes"], 2)
        self.assertEqual(health["files"], 1)
        self.assertEqual(health["graph_revision"], REVISION)
        self.assertEqual(health["schema_version"], "9")
        self.assertTrue(health["durable"])

    def test_stale_zero_missing_and_volatile_graphs_are_explicit(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("update metadata set value=? where key='git_head_sha'", ("b" * 40,))
        stale = self.provider.health()
        self.assertEqual(stale["status"], "stale")
        self.assertIn("revision_mismatch", stale["warnings"])

        self.db.unlink()
        self._build_db(nodes=0)
        zero = self.provider.health()
        self.assertEqual(zero["status"], "unavailable")
        self.assertIn("zero_nodes", zero["warnings"])

        self.db.unlink()
        missing = self.provider.health()
        self.assertEqual(missing["status"], "unavailable")
        self.assertIn("missing_graph_database", missing["warnings"])

        volatile = self.root / "volatile-registry.json"
        volatile.write_text(json.dumps({"repos": [{
            "path": str(self.repo), "data_dir": "/private/tmp/crg-volatile",
        }]}))
        provider = CrgEvidenceProvider(
            repo_root=self.repo, project_id="1" * 16, registry_path=volatile,
            ledger=self.ledger, revision_resolver=lambda _root: REVISION,
        )
        self.assertIn("volatile_data_directory", provider.health()["warnings"])

    @unittest.skipIf(os.name == "nt", "/private/tmp fixture is POSIX-specific")
    def test_symlinked_graph_database_cannot_bypass_durability(self):
        volatile = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(volatile.cleanup)
        target = Path(volatile.name) / "graph.db"
        shutil.copy2(self.db, target)
        self.db.unlink()
        self.db.symlink_to(target)
        health = self.provider.health()
        self.assertEqual(health["status"], "unavailable")
        self.assertFalse(health["durable"])
        self.assertIn("symlink_graph_database", health["warnings"])

    def test_missing_revision_and_invalid_graph_timestamp_are_unavailable(self):
        no_revision = CrgEvidenceProvider(
            repo_root=self.repo, project_id="1" * 16,
            registry_path=self.registry, ledger=self.ledger,
            revision_resolver=lambda _root: "",
        ).health()
        self.assertEqual(no_revision["status"], "unavailable")
        self.assertIn("missing_repository_revision", no_revision["warnings"])

        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "update metadata set value='not-a-time' where key='last_updated'"
            )
        invalid_time = self.provider.health()
        self.assertEqual(invalid_time["status"], "unavailable")
        self.assertIn("invalid_graph_timestamp", invalid_time["warnings"])

    def test_request_generation_covers_every_supported_crg_surface(self):
        expected = {
            "semantic_search": "semantic_search_nodes",
            "graph_query": "query_graph",
            "impact": "get_impact_radius",
            "architecture": "get_architecture_overview",
            "change_review": "detect_changes",
        }
        for operation, tool in expected.items():
            with self.subTest(operation=operation):
                query = "callers_of" if operation == "graph_query" else "order handler"
                request = self.provider.request(
                    operation=operation, query=query, target="OrderHandler",
                )
                self.assertEqual(request["tool_name"], tool)
                self.assertEqual(request["repository_revision"], REVISION)
                self.assertEqual(request["health"]["status"], "healthy")

    def test_missing_graph_returns_warning_and_query_plan_without_evidence(self):
        self.db.unlink()
        request = self.provider.request(operation="semantic_search", query="order")
        self.assertEqual(request["status"], "planned")
        self.assertIn("evidence_unavailable", request["warnings"])
        self.assertNotIn("evidence", request)

    def test_orchestrator_routes_structural_intent_but_skips_non_code_writing(self):
        request = build_evidence_request(
            self.provider, "find callers of OrderHandler in this repository",
        )
        self.assertEqual(request["tool_name"], "query_graph")
        self.assertEqual(request["parameters"]["pattern"], "callers_of")
        self.assertIsNone(build_evidence_request(
            self.provider, "draft non-code documentation",
        ))


class CrgEvidenceRecordTest(CrgEvidenceFixture):
    def test_valid_crg_evidence_records_bounded_revision_bound_provenance(self):
        result = self.provider.record(self.crg_payload())
        self.assertEqual(result["status"], "recorded")
        entry = json.loads(self.ledger.path.read_text().strip())
        provenance = entry["provenance"]
        self.assertEqual(provenance["kind"], "crg_node")
        self.assertEqual(provenance["provider"], "crg")
        self.assertEqual(provenance["repository_revision"], REVISION)
        self.assertEqual(provenance["locator"]["tool_name"], "semantic_search_nodes")
        self.assertNotIn("raw_output", entry)

    def test_revision_file_hash_and_removed_symbol_mismatches_are_rejected(self):
        with self.assertRaisesRegex(CrgEvidenceError, "revision"):
            self.provider.record(self.crg_payload(repository_revision="b" * 40))
        with self.assertRaisesRegex(CrgEvidenceError, "file hash"):
            self.provider.record(self.crg_payload(symbols=[{
                "qualified_name": "pkg/service.py::handle_order",
                "file_path": "pkg/service.py", "file_hash": "sha256:" + "0" * 64,
            }]))
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("delete from nodes where qualified_name like '%handle_order'")
        with self.assertRaisesRegex(CrgEvidenceError, "symbol"):
            self.provider.record(self.crg_payload())

    def test_tested_by_is_candidate_evidence_not_an_executed_test(self):
        result = self.provider.record(self.crg_payload(relationships=["TESTED_BY"]))
        self.assertEqual(result["status"], "recorded")
        entry = json.loads(self.ledger.path.read_text().strip())
        self.assertFalse(entry["verification"]["executed_test"])
        with self.assertRaisesRegex(CrgEvidenceError, "TESTED_BY"):
            self.provider.record_test_run({
                "repository_root": str(self.repo), "repository_revision": REVISION,
                "command_digest": "sha256:" + "c" * 64, "exit_code": 0,
                "completed_at": "2026-07-18T05:00:00Z", "test_ids": ["candidate"],
                "source_relation": "TESTED_BY",
            })
        with self.assertRaisesRegex(CrgEvidenceError, "structural association"):
            self.provider.record_test_run({
                "repository_root": str(self.repo), "repository_revision": REVISION,
                "command_digest": "sha256:" + "c" * 64, "exit_code": 0,
                "completed_at": "2026-07-18T05:00:00Z", "test_ids": ["candidate"],
                "source_relation": "tested_by",
            })

    def test_explicit_test_run_is_recorded_as_test_runner_provenance(self):
        result = self.provider.record_test_run({
            "repository_root": str(self.repo), "repository_revision": REVISION,
            "command_digest": "sha256:" + "c" * 64, "exit_code": 0,
            "completed_at": "2026-07-18T05:00:00Z",
            "test_ids": ["tests/test_orders.py::test_handler"],
        })
        self.assertEqual(result["status"], "recorded")
        entry = json.loads(self.ledger.path.read_text().strip())
        self.assertEqual(entry["provenance"]["kind"], "test_run")
        self.assertEqual(entry["provenance"]["provider"], "test-runner")
        self.assertTrue(entry["verification"]["executed_test"])

    def test_relationships_and_test_ids_reject_sensitive_plaintext(self):
        with self.assertRaisesRegex(CrgEvidenceError, "relationships.*sensitive"):
            self.provider.record(self.crg_payload(
                relationships=["api_key=not-safe-for-a-ledger"],
            ))
        with self.assertRaisesRegex(CrgEvidenceError, "test_ids.*sensitive"):
            self.provider.record_test_run({
                "repository_root": str(self.repo), "repository_revision": REVISION,
                "command_digest": "sha256:" + "c" * 64, "exit_code": 0,
                "completed_at": "2026-07-18T05:00:00Z",
                "test_ids": ["test_auth[Bearer abcdefghijklmnop]"],
            })

    def test_duplicate_cross_process_appends_write_one_owner_only_record(self):
        script = """
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from orchestration.providers.crg_evidence import CrgEvidenceProvider, EvidenceLedger
provider = CrgEvidenceProvider(
    repo_root=sys.argv[2], project_id="1" * 16,
    registry_path=sys.argv[3], ledger=EvidenceLedger(sys.argv[4]),
    revision_resolver=lambda _root: sys.argv[5],
)
print(provider.record(json.loads(sys.argv[6]))["status"])
"""
        arguments = [
            sys.executable, "-c", script, str(MEMORY), str(self.repo),
            str(self.registry), str(self.ledger.path), REVISION,
            json.dumps(self.crg_payload()),
        ]
        workers = [subprocess.Popen(
            arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for _ in range(8)]
        completed = [worker.communicate(timeout=10) for worker in workers]
        self.assertEqual(
            [(worker.returncode, stderr) for worker, (_stdout, stderr) in zip(workers, completed)],
            [(0, "")] * 8,
        )
        results = [stdout.strip() for stdout, _stderr in completed]
        self.assertEqual(results.count("recorded"), 1)
        self.assertEqual(results.count("duplicate"), 7)
        self.assertEqual(len(self.ledger.path.read_text().splitlines()), 1)
        if os.name != "nt":
            self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.ledger.path.parent.stat().st_mode & 0o777, 0o700)

    def test_malformed_existing_ledger_fails_closed(self):
        self.ledger.path.parent.mkdir()
        for malformed in (b"\xff\n", b"[]\n", b"{\"evidence_id\":\""):
            with self.subTest(malformed=malformed):
                self.ledger.path.write_bytes(malformed)
                with self.assertRaisesRegex(CrgEvidenceError, "ledger record"):
                    self.provider.record(self.crg_payload())

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_ledger_rejects_symbolic_link_targets(self):
        self.ledger.path.parent.mkdir()
        target = self.root / "unrelated.jsonl"
        target.write_text("preserve me\n")
        self.ledger.path.symlink_to(target)
        with self.assertRaisesRegex(CrgEvidenceError, "symbolic links"):
            self.provider.record(self.crg_payload())
        self.assertEqual(target.read_text(), "preserve me\n")

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_ledger_rejects_symbolic_link_parent_before_chmod(self):
        actual_parent = self.root / "unrelated-directory"
        actual_parent.mkdir(mode=0o755)
        original_mode = actual_parent.stat().st_mode & 0o777
        linked_parent = self.root / "linked-directory"
        linked_parent.symlink_to(actual_parent, target_is_directory=True)
        ledger = EvidenceLedger(linked_parent / "nested" / "ledger.jsonl")
        provider = CrgEvidenceProvider(
            repo_root=self.repo, project_id="1" * 16,
            registry_path=self.registry, ledger=ledger,
            revision_resolver=lambda _root: REVISION,
        )
        with self.assertRaisesRegex(CrgEvidenceError, "parent.*symbolic link"):
            provider.record(self.crg_payload())
        self.assertEqual(actual_parent.stat().st_mode & 0o777, original_mode)
        self.assertFalse((actual_parent / "nested").exists())

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_ledger_rejects_symlink_ancestor_of_existing_parent(self):
        actual_root = self.root / "redirect-target"
        existing_parent = actual_root / "existing"
        existing_parent.mkdir(parents=True, mode=0o755)
        original_mode = existing_parent.stat().st_mode & 0o777
        linked_root = self.root / "redirect-link"
        linked_root.symlink_to(actual_root, target_is_directory=True)
        ledger = EvidenceLedger(linked_root / "existing" / "ledger.jsonl")
        provider = CrgEvidenceProvider(
            repo_root=self.repo, project_id="1" * 16,
            registry_path=self.registry, ledger=ledger,
            revision_resolver=lambda _root: REVISION,
        )
        with self.assertRaisesRegex(CrgEvidenceError, "parent.*symbolic link"):
            provider.record(self.crg_payload())
        self.assertEqual(existing_parent.stat().st_mode & 0o777, original_mode)
        self.assertFalse((existing_parent / "ledger.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
