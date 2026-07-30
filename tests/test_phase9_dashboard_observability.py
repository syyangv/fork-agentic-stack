from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_manager import dashboard_tui


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "0123456789abcdef"
REVISION = "a" * 40
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.identity import derive_project_identity  # noqa: E402
from orchestration.providers.crg_evidence import (  # noqa: E402
    CrgEvidenceProvider,
    EvidenceLedger,
)


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


class DashboardObservabilityTest(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        agent = root / ".agent"
        (agent / "memory" / "orchestration").mkdir(parents=True)
        (agent / "memory" / "evidence").mkdir(parents=True)
        (agent / "memory" / "candidates").mkdir(parents=True)
        (agent / "install.json").write_text(json.dumps({
            "schema_version": 1,
            "adapters": {},
            "orchestration": {
                "profile": "standard", "phase8_quality_gate": "blocked",
                "memos_mode": "off", "evolution_enabled": False,
                "r7_skill_promoted": False,
            },
        }), encoding="utf-8")
        (agent / "memory" / "orchestration" / "config.json").write_text(json.dumps({
            "schema": "agentic.memory.config.v1", "mode": "off",
            "total_token_budget": 12000,
            "lane_reserves": {"governance": 4800, "behavioral": 4200, "evidence": 3000},
            "project_aliases": {},
        }), encoding="utf-8")
        (agent / "memory" / "evidence" / "ledger.jsonl").write_text(
            json.dumps({"evidence_id": "fresh", "provenance": {"freshness": "fresh"}}) + "\n" +
            json.dumps({"evidence_id": "stale", "provenance": {"freshness": "stale"}}) + "\n",
            encoding="utf-8",
        )
        for name in ("one", "two"):
            (agent / "memory" / "candidates" / f"{name}.json").write_text(
                json.dumps({"id": name, "status": "staged"}), encoding="utf-8",
            )
        database = agent / "runtime" / "memos" / PROJECT_ID / "delivery.sqlite3"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as conn:
            conn.execute("create table deliveries(state text, created_at text)")
            conn.execute("insert into deliveries values ('pending', '2020-01-01T00:00:00Z')")
            conn.execute("create table deferred_completions(state text)")
            conn.execute("create table retrieval_invocations(run_id text, reason text, created_at text)")
            conn.execute("insert into retrieval_invocations values ('run', 'task_start', '2020-01-01T00:00:00Z')")
        return agent

    def make_crg(self, root: Path, target: Path) -> tuple[Path, Path, Path]:
        source = target / "pkg" / "service.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("def handle_order():\n    return True\n", encoding="utf-8")
        file_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        data = root / "graph-data"
        data.mkdir()
        database = data / "graph.db"
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "create table metadata (key text primary key, value text not null)"
            )
            connection.executemany("insert into metadata values (?, ?)", [
                ("schema_version", "9"),
                ("last_updated", "2026-07-18T04:00:00Z"),
                ("git_head_sha", REVISION),
            ])
            connection.execute("""create table nodes (
                id integer primary key, kind text, name text,
                qualified_name text unique, file_path text,
                line_start integer, line_end integer, file_hash text,
                updated_at real
            )""")
            connection.execute(
                "insert into nodes values (1, 'File', 'service.py', ?, ?, 1, 2, ?, 1)",
                ("pkg/service.py", "pkg/service.py", file_hash),
            )
            connection.execute(
                "insert into nodes values (2, 'Function', 'handle_order', ?, ?, 1, 2, ?, 1)",
                ("pkg/service.py::handle_order", "pkg/service.py", file_hash),
            )
        registry = root / "registry.json"
        registry.write_text(json.dumps({
            "repos": [{"path": str(target), "data_dir": str(data), "alias": "target"}],
        }), encoding="utf-8")
        project_id = derive_project_identity(target).project_id
        ledger_path = target / ".agent/memory/evidence/ledger.jsonl"
        ledger_path.write_text("", encoding="utf-8")
        os.chmod(ledger_path, 0o600)
        provider = CrgEvidenceProvider(
            repo_root=target,
            project_id=project_id,
            registry_path=registry,
            ledger=EvidenceLedger(ledger_path),
            revision_resolver=lambda _root: REVISION,
        )
        provider.record({
            "kind": "crg_node",
            "tool_name": "semantic_search_nodes",
            "repository_root": str(target),
            "repository_revision": REVISION,
            "graph_updated_at": "2026-07-18T04:00:00Z",
            "summary": "private source summary must not reach the dashboard",
            "confidence_tier": "high",
            "symbols": [{
                "qualified_name": "pkg/service.py::handle_order",
                "file_path": "pkg/service.py",
                "file_hash": "sha256:" + file_hash,
            }],
            "relationships": [],
        })
        return registry, database, source

    def test_panels_are_bounded_read_only_and_consistent_with_plain_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            before = snapshot(root)
            model = dashboard_tui.collect_dashboard(root, ROOT)
            rendered = dashboard_tui.render_plain(root, ROOT, width=120, section="Orchestration")
            self.assertEqual(before, snapshot(root))

        observability = model["orchestration"]
        self.assertEqual(observability["lanes"]["governance"]["status"], "healthy")
        self.assertEqual(observability["lanes"]["behavioral"]["status"], "expected_disabled")
        self.assertEqual(observability["event_lag"]["pending"], 1)
        self.assertEqual(observability["retrieval_latency"]["status"], "unavailable")
        self.assertEqual(observability["retrieval_latency"]["reason"], "no_latency_samples")
        self.assertEqual(observability["stale_links"]["status"], "unavailable")
        self.assertEqual(observability["review_backlog"]["staged"], 2)
        self.assertIn("Orchestration", rendered)
        self.assertIn("Event delivery lag", rendered)
        self.assertIn("Retrieval latency", rendered)
        for name, lane in observability["lanes"].items():
            budget = lane["budget"] if lane["budget"] is not None else "unavailable"
            self.assertIn(f"{name:<12} {lane['status']:<18} budget={budget}", rendered)
        lag = observability["event_lag"]
        self.assertIn(
            f"pending={lag['pending']} inflight={lag['inflight']} "
            f"deferred={lag['deferred']} ambiguous={lag['ambiguous']} dead={lag['dead']}",
            rendered,
        )
        rendered_lag = re.search(
            r"projects=(\d+) oldest_age_seconds=(\d+) "
            r"reason=([a-z_]+) truncated=(True|False)",
            rendered,
        )
        self.assertIsNotNone(rendered_lag)
        assert rendered_lag is not None
        self.assertEqual(int(rendered_lag.group(1)), lag["projects"])
        self.assertLessEqual(
            abs(int(rendered_lag.group(2)) - lag["oldest_age_seconds"]),
            1,
        )
        self.assertEqual(rendered_lag.group(3), lag["reason"])
        self.assertEqual(rendered_lag.group(4), str(lag["truncated"]))
        latency = observability["retrieval_latency"]
        self.assertIn(
            f"samples={latency['samples']} p50_ms=unavailable p95_ms=unavailable "
            f"reason={latency['reason']}",
            rendered,
        )
        stale = observability["stale_links"]
        self.assertIn(
            f"records={stale['records']} current={stale['current']} "
            f"stale={stale['stale']} malformed={stale['malformed']} "
            f"truncated={stale['truncated']}",
            rendered,
        )
        backlog = observability["review_backlog"]
        self.assertIn(
            f"staged={backlog['staged']} malformed={backlog['malformed']} "
            f"bounded={backlog['bounded']}",
            rendered,
        )

    def test_missing_or_malformed_state_degrades_without_fabricated_metrics_or_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_project(root)
            (agent / "memory" / "orchestration" / "config.json").write_text("[\"SECRET_SENTINEL\"]")
            (agent / "memory" / "evidence" / "ledger.jsonl").write_text("not-json\n")
            model = dashboard_tui.collect_dashboard(root, ROOT)
            rendered = dashboard_tui.render_plain(root, ROOT, width=120, section="Orchestration")
            (agent / "install.json").write_text("{invalid-install", encoding="utf-8")
            invalid_install = dashboard_tui.collect_dashboard(root, ROOT)

        self.assertEqual(model["orchestration"]["lanes"]["governance"]["status"], "unavailable")
        self.assertEqual(model["orchestration"]["stale_links"]["status"], "unavailable")
        self.assertEqual(
            invalid_install["orchestration"]["lanes"]["governance"]["status"],
            "unavailable",
        )
        self.assertNotIn("SECRET_SENTINEL", rendered)
        self.assertNotIn("not-json", rendered)

    def test_phase8_active_or_malformed_states_never_render_healthy(self) -> None:
        cases = (
            ({"memos_mode": "assist"}, None),
            ({"evolution_enabled": True}, None),
            ({"r7_skill_promoted": True}, None),
            ({"profile": "unknown"}, None),
            ({}, {"mode": "shadow"}),
        )
        for state_change, config_change in cases:
            with self.subTest(state_change=state_change, config_change=config_change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                agent = self.make_project(root)
                state = json.loads((agent / "install.json").read_text())
                state["orchestration"].update(state_change)
                (agent / "install.json").write_text(json.dumps(state))
                if config_change:
                    config = json.loads((agent / "memory/orchestration/config.json").read_text())
                    config.update(config_change)
                    (agent / "memory/orchestration/config.json").write_text(json.dumps(config))
                lanes = dashboard_tui.collect_dashboard(root, ROOT)["orchestration"]["lanes"]
                self.assertNotEqual(lanes["governance"]["status"], "healthy")
                self.assertNotEqual(lanes["behavioral"]["status"], "expected_disabled")

    def test_truncated_invalid_runtime_and_symlinked_latency_root_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_project(root)
            runtime = agent / "runtime" / "memos"
            for index in range(dashboard_tui._MAX_DASHBOARD_ROWS + 1):
                (runtime / f"not-a-project-{index}").mkdir()
            lag = dashboard_tui._read_event_lag(agent)
            self.assertEqual(lag["status"], "degraded")
            self.assertTrue(lag["truncated"])
            outside = root / "outside"
            outside.mkdir()
            runtime.rename(agent / "runtime" / "memos-saved")
            os.symlink(outside, runtime)
            self.assertFalse(dashboard_tui._retrieval_latency(agent)["journal_present"])

    def test_dashboard_uses_trusted_dynamic_crg_audit_without_content_or_target_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_project(root)
            registry, database, _source = self.make_crg(root, root)
            sentinel = root / "target-code-executed"
            (agent / "memory/orchestration/__init__.py").write_text(
                f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            environment = {
                "AGENTIC_CRG_REGISTRY": str(registry),
                "AGENTIC_CRG_REVISION": REVISION,
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                model = dashboard_tui.collect_dashboard(root, ROOT)
                rendered = dashboard_tui.render_plain(
                    root, ROOT, width=120, section="Orchestration",
                )
            stale = model["orchestration"]["stale_links"]
            self.assertEqual(stale["status"], "healthy")
            self.assertEqual(stale["current"], 1)
            self.assertEqual(stale["stale"], 0)
            self.assertFalse(sentinel.exists())
            self.assertNotIn("private source summary", json.dumps(model))
            self.assertNotIn("handle_order", json.dumps(model))
            self.assertNotIn("private source summary", rendered)

            with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "update metadata set value=? where key='last_updated'",
                    ("2026-07-18T05:00:00Z",),
                )
            with mock.patch.dict(os.environ, environment, clear=False):
                drifted = dashboard_tui.collect_dashboard(root, ROOT)
            self.assertEqual(drifted["orchestration"]["stale_links"]["stale"], 1)

    def test_live_wal_journal_topology_bytes_and_mtimes_are_not_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_project(root)
            database = agent / "runtime/memos" / PROJECT_ID / "delivery.sqlite3"
            writer = sqlite3.connect(database)
            self.addCleanup(writer.close)
            self.assertEqual(writer.execute("pragma journal_mode=wal").fetchone()[0], "wal")
            writer.execute(
                "insert into deliveries values ('pending', '2020-01-02T00:00:00Z')"
            )
            writer.commit()
            wal = database.with_name(database.name + "-wal")
            shm = database.with_name(database.name + "-shm")
            self.assertTrue(wal.is_file())
            self.assertTrue(shm.is_file())
            before_paths = {
                path.relative_to(root).as_posix() for path in root.rglob("*")
            }
            before = snapshot(root)

            model = dashboard_tui.collect_dashboard(root, ROOT)
            dashboard_tui.render_plain(root, ROOT, width=120, section="Orchestration")

            self.assertEqual(model["orchestration"]["event_lag"]["pending"], 2)
            self.assertEqual(before_paths, {
                path.relative_to(root).as_posix() for path in root.rglob("*")
            })
            self.assertEqual(before, snapshot(root))

    def test_runtime_database_topology_race_degrades_instead_of_reporting_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_project(root)
            original = dashboard_tui._copy_regular_snapshot

            def inject_wal(source: Path, destination: Path, maximum: int) -> None:
                original(source, destination, maximum)
                if source.name == "delivery.sqlite3":
                    raced = source.with_name(source.name + "-wal")
                    raced.write_bytes(b"changed topology")
                    os.chmod(raced, 0o600)

            with mock.patch.object(
                dashboard_tui, "_copy_regular_snapshot", side_effect=inject_wal,
            ):
                lag = dashboard_tui._read_event_lag(agent)
            self.assertEqual(lag["status"], "degraded")
            self.assertEqual(lag["reason"], "journal_unreadable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
