import importlib.util
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / ".agent" / "memory"
TOOLS = ROOT / ".agent" / "tools"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _reject_candidate(candidates_dir: str, candidate_id: str):
    sys.path.insert(0, str(MEMORY))
    from review_state import mark_rejected

    try:
        mark_rejected(candidate_id, "test-reviewer", "test", candidates_dir)
    except FileNotFoundError:
        pass


class ScheduledReviewPolicyTest(unittest.TestCase):
    def test_scheduled_triage_can_reject_but_never_graduate(self):
        policy = load_module(TOOLS / "scheduled_review_policy.py", "scheduled_policy")
        candidates = [
            {"id": "noise", "claim": "Wrote settings.json (3 lines)"},
            {
                "id": "strong",
                "claim": "Always retry the flaky operation",
                "canonical_salience": 10,
                "cluster_size": 9,
                "conditions": ["a", "b", "c", "d", "e"],
            },
        ]
        decision = policy.triage_candidates(candidates)
        self.assertEqual([c["id"] for c in decision.rejected], ["noise"])
        self.assertEqual([c["id"] for c in decision.needs_review], ["strong"])
        self.assertFalse(hasattr(decision, "graduated"))
        self.assertNotIn("graduate.py", (TOOLS / "scheduled_review_policy.py").read_text())

    def test_installed_scheduler_has_no_graduation_path_when_present(self):
        scheduler = Path.home() / "Library" / "Scripts" / "agentic_stack_review_notify.py"
        if not scheduler.is_file():
            self.skipTest("host scheduler is not installed")
        text = scheduler.read_text(encoding="utf-8")
        self.assertNotIn("graduate.py", text)
        self.assertNotIn("auto_graduated", text)


class CandidateSerializationTest(unittest.TestCase):
    def test_human_rejection_is_terminal_until_explicit_reopen(self):
        sys.path.insert(0, str(MEMORY))
        from promote import write_candidates
        from review_state import mark_rejected, mark_reopened

        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp) / "memory"
            candidates = memory / "candidates"
            (memory / "working").mkdir(parents=True)
            (memory / "semantic").mkdir()
            (memory / "semantic" / "LESSONS.md").write_text("# Lessons\n")
            candidates.mkdir()
            original = {
                "id": "terminal",
                "claim": "Keep human rejection terminal until explicit reopen.",
                "status": "staged",
                "staged_at": "2026-07-16T00:00:00+00:00",
                "evidence_ids": ["old"],
                "canonical_salience": 8,
                "cluster_size": 2,
                "decisions": [],
            }
            (candidates / "terminal.json").write_text(json.dumps(original))
            mark_rejected("terminal", "host-agent", "not doctrine", str(candidates))

            pattern = {
                "id": "terminal",
                "name": "terminal",
                "claim": original["claim"],
                "conditions": ["review"],
                "evidence_ids": ["old", "new"],
                "canonical_salience": 9,
                "cluster_size": 3,
            }
            self.assertEqual(write_candidates({"terminal": pattern}, str(candidates)), 0)
            self.assertFalse((candidates / "terminal.json").exists())
            self.assertTrue((candidates / "rejected" / "terminal.json").exists())

            mark_reopened("terminal", "host-agent", str(candidates))
            self.assertTrue((candidates / "terminal.json").exists())
            self.assertEqual(write_candidates({"terminal": pattern}, str(candidates)), 1)
            staged = json.loads((candidates / "terminal.json").read_text())
            self.assertEqual(staged["status"], "staged")
            self.assertTrue(any(d.get("action") == "reopened" for d in staged["decisions"]))

    def test_parallel_mutations_leave_one_terminal_record_and_fresh_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp) / "memory"
            candidates = memory / "candidates"
            (candidates / "rejected").mkdir(parents=True)
            (memory / "working").mkdir()
            candidate = {
                "id": "same",
                "claim": "Serialize candidate lifecycle writes.",
                "status": "staged",
                "staged_at": "2026-07-16T00:00:00+00:00",
                "canonical_salience": 8,
                "cluster_size": 2,
                "decisions": [],
            }
            (candidates / "same.json").write_text(json.dumps(candidate))

            workers = [
                multiprocessing.Process(
                    target=_reject_candidate, args=(str(candidates), "same")
                )
                for _ in range(8)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)

            locations = list(candidates.glob("same.json")) + list(
                (candidates / "rejected").glob("same.json")
            )
            self.assertEqual(len(locations), 1)
            terminal = json.loads(locations[0].read_text())
            self.assertEqual(terminal["status"], "rejected")
            queue = (memory / "working" / "REVIEW_QUEUE.md").read_text()
            self.assertIn("No pending candidates", queue)


class DreamStateTest(unittest.TestCase):
    def test_dream_clustering_window_is_bounded_without_mutating_history(self):
        sys.path.insert(0, str(MEMORY))
        module = load_module(MEMORY / "auto_dream.py", "auto_dream_window")
        module.MAX_CLUSTER_ENTRIES = 3
        entries = [{"id": value} for value in range(7)]
        selected = module._entries_for_clustering(entries)
        self.assertEqual([row["id"] for row in selected], [4, 5, 6])
        self.assertEqual(len(entries), 7)

    def test_success_is_recorded_only_after_cycle_body_finishes(self):
        state_mod = load_module(MEMORY / "dream_state.py", "dream_state_success")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dream-state.json"
            run = state_mod.start_cycle(path)
            entered = json.loads(path.read_text())
            self.assertEqual(entered["last_status"], "running")
            self.assertIsNone(entered.get("last_success_at"))
            state_mod.finish_cycle(path, run, started_monotonic=0.0, now_monotonic=1.25)
            completed = json.loads(path.read_text())
            self.assertEqual(completed["last_status"], "success")
            self.assertIsNotNone(completed["last_success_at"])
            self.assertEqual(completed["last_duration_ms"], 1250)

    def test_failure_has_distinct_timestamp_and_error(self):
        state_mod = load_module(MEMORY / "dream_state.py", "dream_state_failure")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dream-state.json"
            run = state_mod.start_cycle(path)
            state_mod.fail_cycle(
                path, run, RuntimeError("boom"), started_monotonic=2.0, now_monotonic=2.5
            )
            failed = json.loads(path.read_text())
            self.assertEqual(failed["last_status"], "failure")
            self.assertIsNotNone(failed["last_failure_at"])
            self.assertEqual(failed["last_duration_ms"], 500)
            self.assertEqual(failed["last_error"], "RuntimeError: boom")


class CrgHealthTest(unittest.TestCase):
    def _db(self, directory: Path, nodes: int):
        directory.mkdir(parents=True)
        with sqlite3.connect(directory / "graph.db") as conn:
            conn.execute("create table nodes (id integer primary key)")
            conn.executemany("insert into nodes default values", [()] * nodes)

    def test_volatile_missing_and_zero_node_graphs_are_unhealthy(self):
        from harness_manager.crg_health import inspect_registration

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            missing = inspect_registration({"path": str(repo), "data_dir": str(root / "missing")})
            self.assertEqual(missing.status, "red")
            self.assertIn("missing", missing.reasons[0])

            empty = root / "empty"
            self._db(empty, 0)
            zero = inspect_registration({"path": str(repo), "data_dir": str(empty)})
            self.assertEqual(zero.status, "red")
            self.assertIn("zero nodes", zero.reasons)

        volatile = inspect_registration(
            {"path": "/repo", "data_dir": "/private/tmp/crg-data"},
            require_repo=False,
        )
        self.assertEqual(volatile.status, "red")
        self.assertIn("volatile data directory", volatile.reasons)


class InfrastructureManifestTest(unittest.TestCase):
    def test_manifest_declares_phase0_trust_features(self):
        manifest = json.loads((ROOT / ".agent" / "infrastructure.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["orchestration_phase"], 0)
        self.assertIn("serialized_candidate_lifecycle", manifest["features"])
        self.assertIn("structured_dream_health", manifest["features"])
        self.assertIn("latest_state_recall", manifest["features"])


if __name__ == "__main__":
    unittest.main()
