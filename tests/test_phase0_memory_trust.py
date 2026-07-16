import importlib.util
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
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
    def test_rejection_move_failure_recovers_without_duplicate_lifecycle_files(self):
        sys.path.insert(0, str(MEMORY))
        import review_state

        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp) / "memory"
            candidates = memory / "candidates"
            (memory / "working").mkdir(parents=True)
            candidates.mkdir()
            candidate = {
                "id": "recover",
                "claim": "Recover interrupted lifecycle moves atomically.",
                "status": "staged",
                "staged_at": "2026-07-16T00:00:00+00:00",
                "decisions": [],
            }
            (candidates / "recover.json").write_text(json.dumps(candidate))

            with mock.patch.object(review_state, "_move_candidate", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    review_state.mark_rejected(
                        "recover", "host-agent", "not doctrine", str(candidates)
                    )

            self.assertTrue((candidates / "recover.json").exists())
            self.assertFalse((candidates / "rejected" / "recover.json").exists())
            interrupted = json.loads((candidates / "recover.json").read_text())
            self.assertEqual(interrupted["status"], "rejected")

            review_state.mark_rejected(
                "recover", "host-agent", "not doctrine", str(candidates)
            )
            self.assertFalse((candidates / "recover.json").exists())
            terminal_path = candidates / "rejected" / "recover.json"
            self.assertTrue(terminal_path.exists())
            terminal = json.loads(terminal_path.read_text())
            rejected = [d for d in terminal["decisions"] if d.get("action") == "rejected"]
            self.assertEqual(len(rejected), 1)

    def test_automated_restage_move_failure_recovers_without_duplicate_files(self):
        sys.path.insert(0, str(MEMORY))
        import promote
        from review_state import mark_rejected

        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp) / "memory"
            candidates = memory / "candidates"
            (memory / "working").mkdir(parents=True)
            (memory / "semantic").mkdir()
            (memory / "semantic" / "LESSONS.md").write_text("# Lessons\n")
            candidates.mkdir()
            candidate = {
                "id": "restage",
                "claim": "Retry deterministic rejection after new evidence.",
                "status": "staged",
                "staged_at": "2026-07-16T00:00:00+00:00",
                "evidence_ids": ["old"],
                "decisions": [],
            }
            (candidates / "restage.json").write_text(json.dumps(candidate))
            mark_rejected(
                "restage", "heuristic_prefilter", "deterministic", str(candidates)
            )
            pattern = {
                "id": "restage",
                "name": "restage",
                "claim": candidate["claim"],
                "conditions": ["review"],
                "evidence_ids": ["old", "new"],
                "canonical_salience": 9,
                "cluster_size": 3,
            }
            with mock.patch.object(promote, "_move_candidate", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    promote.write_candidates({"restage": pattern}, str(candidates))
            self.assertFalse((candidates / "restage.json").exists())
            interrupted = candidates / "rejected" / "restage.json"
            self.assertEqual(json.loads(interrupted.read_text())["status"], "staged")

            self.assertEqual(promote.write_candidates({"restage": pattern}, str(candidates)), 1)
            self.assertTrue((candidates / "restage.json").exists())
            self.assertFalse(interrupted.exists())

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
    def test_locked_rewrite_retries_short_writes_and_fsyncs(self):
        sys.path.insert(0, str(MEMORY))
        module = load_module(MEMORY / "auto_dream.py", "auto_dream_short_write")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "episodes.jsonl"
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            self.addCleanup(os.close, fd)
            original_write = os.write
            calls = 0

            def short_once(target_fd, data):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original_write(target_fd, bytes(data[:2]))
                return original_write(target_fd, bytes(data))

            with mock.patch.object(module.os, "write", side_effect=short_once), mock.patch.object(
                module.os, "fsync", wraps=os.fsync
            ) as fsync:
                module._write_entries_locked(fd, [{"id": 1}, {"id": 2}])
            self.assertGreaterEqual(calls, 2)
            fsync.assert_called_once_with(fd)
            self.assertEqual(
                [json.loads(line) for line in path.read_text().splitlines()],
                [{"id": 1}, {"id": 2}],
            )

    def test_unwritable_legacy_markers_do_not_block_or_leave_running_state(self):
        sys.path.insert(0, str(MEMORY))
        module = load_module(MEMORY / "auto_dream.py", "auto_dream_marker_failure")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "dream-state.json"
            marker_dir = root / "marker-is-directory"
            marker_dir.mkdir()
            module.DREAM_STATE = str(state)
            module.STOP_ENTRY_MARKER = str(marker_dir)
            module.STOP_COMPLETION_MARKER = str(marker_dir)
            ran = []
            module.run_dream_cycle = lambda: ran.append(True)
            module.main()
            health = json.loads(state.read_text())
            self.assertEqual(ran, [True])
            self.assertEqual(health["last_status"], "success")

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


class ScheduledReviewHealthTest(unittest.TestCase):
    def test_doctor_fails_unsafe_legacy_scheduler_under_arbitrary_home(self):
        from harness_manager import doctor

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scheduler = (
                home / "Library" / "Scripts" / "agentic_stack_review_notify.py"
            )
            scheduler.parent.mkdir(parents=True)
            scheduler.write_text('subprocess.run(["graduate.py", candidate])\n')
            messages = []
            self.assertEqual(
                doctor._audit_scheduled_reviewer(log=messages.append, home=home), 1
            )
            self.assertIn("automatic acceptance is forbidden", "\n".join(messages))

            scheduler.write_text("from scheduled_review_policy import triage_candidates\n")
            self.assertEqual(
                doctor._audit_scheduled_reviewer(log=messages.append, home=home), 0
            )


if __name__ == "__main__":
    unittest.main()
