import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / ".agent"
sys.path.insert(0, str(AGENT / "memory"))
from orchestration.contracts import EventEnvelope
from orchestration.memos_journal import JournalConflict, MemosDeliveryJournal
from orchestration.orchestrator import build_shadow_packet
from orchestration.providers.memos_local import MemosLocalProvider
from orchestration.providers.governance import GovernanceProvider
sys.path.insert(0, str(AGENT / "harness"))
from text import word_set


class FakeClient:
    def __init__(self, failures=None):
        self.calls = []
        self.timeouts = []
        self.failures = dict(failures or {})

    def call(self, method, params, *, timeout=None, retryable=False):
        self.calls.append((method, params, retryable))
        self.timeouts.append((method, timeout))
        failure = self.failures.pop(method, None)
        if failure:
            raise failure
        if method == "core.health":
            return {"ok": True, "version": "2.0.10"}
        if method == "memory.timeline":
            return {"traces": [{"id": "tr_1", "summary": "safe trace"}]}
        if method == "skill.list":
            return {"skills": []}
        if method == "memory.list_world_models":
            return {"worldModels": []}
        if method == "turn.start":
            return {"query": {"episodeId": "ep-real-1"}, "hits": []}
        return {"ok": True}

    def health(self, *, timeout=None):
        self.calls.append(("core.health", None, True))
        self.timeouts.append(("core.health", timeout))
        return {"ok": True, "version": "2.0.10", "capabilities": ("core.health",)}


def event(event_type, *, payload=None, suffix="1"):
    return EventEnvelope.create(
        timestamp="2026-07-16T20:00:00Z",
        event_type=event_type,
        project_id="0123456789abcdef",
        repo_root="/repo",
        revision="abcdef1",
        harness="codex",
        run_id="run-1",
        session_id="session-1",
        actor="tool" if event_type == "tool.completed" else "agent",
        intent="implement memory adapter",
        payload=payload or {},
        idempotency_key=f"idem-{event_type}-{suffix}",
    )


class JournalTest(unittest.TestCase):
    def test_duplicate_delivery_is_idempotent_and_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3")
            self.assertTrue(journal.enqueue("event", "idem", "core.health", {"x": 1}, True))
            self.assertFalse(journal.enqueue("event", "idem", "core.health", {"x": 1}, True))
            with self.assertRaises(JournalConflict):
                journal.enqueue("event", "idem", "core.health", {"x": 2}, True)

    def test_nonretryable_ambiguous_failure_is_never_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3")
            journal.enqueue("event", "idem", "feedback.submit", {"magnitude": 1}, False)
            claimed = journal.claim_next()
            journal.mark_failed(claimed.delivery_id, "connection lost", ambiguous=True)
            self.assertIsNone(journal.claim_next())
            self.assertEqual(journal.counts()["ambiguous"], 1)

    def test_nonretryable_unambiguous_transient_failure_is_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3")
            journal.enqueue("event", "idem", "turn.end", {}, False)
            claimed = journal.claim_next()
            state = journal.mark_failed(
                claimed.delivery_id, "broken pipe before write", ambiguous=False,
                retryable_failure=True,
            )
            self.assertEqual(state, "pending")
            self.assertIsNotNone(journal.claim_next())

    def test_permanent_upstream_failure_is_not_retried_by_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3")
            journal.enqueue("event", "idem", "session.open", {}, True)
            claimed = journal.claim_next()
            state = journal.mark_failed(
                claimed.delivery_id, "invalid params", ambiguous=False,
                retryable_failure=False,
            )
            self.assertEqual(state, "dead")
            self.assertIsNone(journal.claim_next())

    def test_journal_schema_has_delivery_cursor_and_unique_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            journal = MemosDeliveryJournal(path)
            journal.enqueue("event", "idem", "session.open", {}, True)
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "select sequence, state, attempts from deliveries"
                ).fetchone()
            self.assertEqual(row, (1, "pending", 0))

    def test_run_lifecycle_preserves_returned_upstream_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3")
            journal.begin_run("run-1", "session-1")
            journal.set_episode("run-1", "ep-upstream")
            self.assertEqual(journal.lifecycle("run-1"), {
                "session_id": "session-1", "episode_id": "ep-upstream",
            })
            journal.begin_run("run-1", "session-1")
            with self.assertRaises(JournalConflict):
                journal.set_episode("run-1", "ep-different")

    def test_interrupted_inflight_delivery_recovers_conservatively(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            journal = MemosDeliveryJournal(path)
            journal.enqueue("a", "safe", "session.open", {}, True)
            journal.enqueue("b", "unsafe", "turn.end", {}, False)
            journal.claim_next()
            journal.claim_next()
            live = MemosDeliveryJournal(path)
            self.assertEqual(live.counts()["inflight"], 2)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "update deliveries set updated_at='2000-01-01T00:00:00Z'"
                )
                connection.commit()
            recovered = MemosDeliveryJournal(path)
            with recovered.delivery_worker():
                self.assertEqual(recovered.counts()["pending"], 1)
                self.assertEqual(recovered.counts()["ambiguous"], 1)

    def test_crash_recovery_respects_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            journal = MemosDeliveryJournal(path, max_attempts=1)
            journal.enqueue("event", "idem", "session.open", {}, True)
            journal.claim_next()
            recovered = MemosDeliveryJournal(path, max_attempts=1)
            with recovered.delivery_worker():
                self.assertEqual(recovered.counts()["pending"], 0)
                self.assertEqual(recovered.counts()["dead"], 1)

    def test_delivery_worker_lock_serializes_real_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            marker = Path(tmp) / "order.log"
            code = (
                "import sys,time; from pathlib import Path; "
                f"sys.path.insert(0,{str(AGENT / 'memory')!r}); "
                "from orchestration.memos_journal import MemosDeliveryJournal; "
                "j=MemosDeliveryJournal(sys.argv[1]); m=Path(sys.argv[2]); "
                "ctx=j.delivery_worker(); ctx.__enter__(); "
                "m.open('a').write(sys.argv[3]+'-enter\\n'); time.sleep(0.2); "
                "m.open('a').write(sys.argv[3]+'-exit\\n'); ctx.__exit__(None,None,None)"
            )
            first = subprocess.Popen(
                [sys.executable, "-c", code, str(path), str(marker), "a"]
            )
            second = subprocess.Popen(
                [sys.executable, "-c", code, str(path), str(marker), "b"]
            )
            self.assertEqual(first.wait(timeout=2), 0)
            self.assertEqual(second.wait(timeout=2), 0)
            lines = marker.read_text().splitlines()
            self.assertIn(lines, ([
                "a-enter", "a-exit", "b-enter", "b-exit",
            ], [
                "b-enter", "b-exit", "a-enter", "a-exit",
            ]))

    def test_concurrent_duplicate_and_conflict_writes_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            first = MemosDeliveryJournal(path)
            second = MemosDeliveryJournal(path)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda journal: journal.enqueue(
                        "event", "same", "session.open", {"x": 1}, True,
                    ),
                    (first, second),
                ))
            self.assertEqual(sorted(results), [False, True])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(journal.begin_run, "run", session)
                    for journal, session in ((first, "a"), (second, "b"))
                ]
            errors = [future.exception() for future in futures]
            self.assertEqual(sum(isinstance(error, JournalConflict) for error in errors), 1)

    def test_concurrent_deferred_completion_writes_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            first = MemosDeliveryJournal(path)
            second = MemosDeliveryJournal(path)
            completed = event("task.completed", payload={"outcome_summary": "done"})
            args = (
                completed.run_id, completed.event_id,
                completed.idempotency_key, completed.to_dict(),
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [
                    future.result() for future in (
                        pool.submit(first.defer_completion, *args),
                        pool.submit(second.defer_completion, *args),
                    )
                ]
            self.assertEqual(sorted(results), [False, True])

            conflicting = dict(completed.to_dict())
            conflicting["intent"] = "different"
            with self.assertRaises(JournalConflict):
                second.defer_completion(
                    completed.run_id, completed.event_id,
                    completed.idempotency_key, conflicting,
                )
            with self.assertRaises(JournalConflict):
                second.defer_completion(
                    "different-run", completed.event_id,
                    completed.idempotency_key, completed.to_dict(),
                )

    def test_completion_materialization_is_atomic_on_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3")
            completed = event("task.completed", payload={"outcome_summary": "done"})
            journal.defer_completion(
                completed.run_id, completed.event_id,
                completed.idempotency_key, completed.to_dict(),
            )
            journal.enqueue(
                "other", completed.idempotency_key, "episode.close",
                {"episodeId": "wrong"}, True,
            )
            deliveries = [
                ("turn.end", {"episodeId": "right"}, False),
                ("episode.close", {"episodeId": "right"}, True),
            ]
            with self.assertRaises(JournalConflict):
                journal.materialize_completion(
                    completed.run_id, completed.event_id,
                    completed.idempotency_key, deliveries,
                )
            self.assertIsNotNone(journal.deferred_completion(completed.run_id))
            with closing(sqlite3.connect(journal.path)) as connection:
                count = connection.execute(
                    "select count(*) from deliveries where method='turn.end'"
                ).fetchone()[0]
            self.assertEqual(count, 0)


class ProviderTest(unittest.TestCase):
    def test_manifest_declares_phase3_shadow_features(self):
        manifest = json.loads((AGENT / "infrastructure.json").read_text())
        self.assertGreaterEqual(manifest["orchestration_phase"], 3)
        self.assertTrue({
            "memos_local_artifact_pin", "memos_shadow_provider",
            "memos_delivery_journal", "memos_bridge_supervision",
            "memos_project_isolation", "memos_privacy_config",
        }.issubset(manifest["features"]))

    def provider(self, tmp, client=None):
        return MemosLocalProvider(
            project_id="0123456789abcdef",
            journal=MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3"),
            client=client,
            mode="shadow",
        )

    def test_task_start_maps_full_lifecycle_and_replay_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            provider = self.provider(tmp, client)
            started = event("task.started")
            first = provider.record(started)
            second = provider.record(started)
            methods = [value[0] for value in client.calls]
            self.assertEqual(methods, ["core.health", "session.open", "turn.start"])
            self.assertEqual(first["delivered"], 2)
            self.assertEqual(second["delivered"], 0)
            turn = client.calls[-1][1]
            self.assertEqual(turn["userText"], started.intent)
            self.assertNotIn("episodeId", turn)
            self.assertNotIn("payload", turn)
            self.assertEqual(provider.health()["mode"], "shadow")

    def test_concurrent_workers_preserve_fifo_lifecycle_order(self):
        class BlockingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def call(self, method, params, *, timeout=None, retryable=False):
                if method == "session.open" and not self.entered.is_set():
                    self.entered.set()
                    self.release.wait(2)
                return super().call(
                    method, params, timeout=timeout, retryable=retryable,
                )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            first_client = BlockingClient()
            second_client = FakeClient()
            first = MemosLocalProvider(
                project_id="0123456789abcdef",
                journal=MemosDeliveryJournal(path), client=first_client, mode="shadow",
            )
            second = MemosLocalProvider(
                project_id="0123456789abcdef",
                journal=MemosDeliveryJournal(path), client=second_client, mode="shadow",
            )
            second_event = EventEnvelope.create(
                **{
                    **event("task.started", suffix="2").to_dict(),
                    "event_id": None,
                    "run_id": "run-2",
                    "session_id": "session-2",
                    "idempotency_key": "idem-task.started-2",
                }
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(first.record, event("task.started"))
                self.assertTrue(first_client.entered.wait(1))
                second_future = pool.submit(second.record, second_event)
                time.sleep(0.05)
                self.assertFalse(any(
                    method != "core.health" for method, _, _ in second_client.calls
                ))
                first_client.release.set()
                first_future.result(timeout=2)
                second_future.result(timeout=2)
            lifecycle = [
                method for method, _, _ in first_client.calls
                if method != "core.health"
            ]
            self.assertEqual(lifecycle, [
                "session.open", "turn.start", "session.open", "turn.start",
            ])

    def test_tool_events_are_bounded_and_flushed_through_turn_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            provider = self.provider(tmp, client)
            provider.record(event("task.started"))
            provider.record(event("tool.completed", payload={
                "tool_name": "pytest",
                "input_summary": "focused tests",
                "output_summary": "12 passed",
                "raw_stdout": "must not cross provider boundary",
                "error_code": None,
            }))
            provider.record(event("task.completed", payload={"outcome_summary": "implemented and verified"}))
            methods = [value[0] for value in client.calls]
            self.assertEqual(methods[-3:], ["turn.end", "episode.close", "session.close"])
            turn = client.calls[-3][1]
            self.assertEqual(turn["episodeId"], "ep-real-1")
            self.assertFalse(client.calls[-3][2])
            self.assertEqual(turn["agentText"], "implemented and verified")
            self.assertEqual(turn["toolCalls"][0]["name"], "pytest")
            rendered = json.dumps(turn)
            self.assertNotIn("raw_stdout", rendered)
            self.assertNotIn("must not cross", rendered)

    def test_feedback_is_nonretryable_and_unknown_after_transport_loss(self):
        class LostConnection(RuntimeError):
            ambiguous = True

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient({"feedback.submit": LostConnection("lost")})
            provider = self.provider(tmp, client)
            result = provider.record(event("feedback.recorded", payload={
                "polarity": "negative", "magnitude": 0.8, "rationale": "wrong approach"
            }))
            self.assertEqual(result["ambiguous"], 1)
            self.assertEqual(len(client.calls), 2)
            self.assertFalse(client.calls[-1][2])
            self.assertEqual(client.calls[-1][1]["channel"], "explicit")
            self.assertEqual(provider.journal.counts()["ambiguous"], 1)

    def test_nonretryable_postwrite_protocol_failure_is_ambiguous(self):
        from orchestration.memos_bridge import MemOSProtocolError

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient({
                "feedback.submit": MemOSProtocolError(
                    "malformed response", ambiguous=True,
                ),
            })
            provider = self.provider(tmp, client)
            result = provider.record(event("feedback.recorded", payload={
                "polarity": "negative", "magnitude": 1,
            }))
            self.assertEqual(result["ambiguous"], 1)
            self.assertEqual(provider.journal.counts()["ambiguous"], 1)

    def test_successful_turn_start_with_malformed_identity_is_ambiguous(self):
        class MalformedStart(FakeClient):
            def call(self, method, params, *, timeout=None, retryable=False):
                if method == "turn.start":
                    self.calls.append((method, params, retryable))
                    self.timeouts.append((method, timeout))
                    return {"hits": []}
                return super().call(
                    method, params, timeout=timeout, retryable=retryable,
                )

        with tempfile.TemporaryDirectory() as tmp:
            provider = self.provider(tmp, MalformedStart())
            result = provider.record(event("task.started"))
            self.assertEqual(result["ambiguous"], 1)
            self.assertEqual(provider.journal.counts()["ambiguous"], 1)

    def test_successful_turn_start_with_mapping_failure_is_ambiguous(self):
        class MappingFailureJournal(MemosDeliveryJournal):
            def set_episode(self, run_id, episode_id):
                raise sqlite3.OperationalError("mapping unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            provider = MemosLocalProvider(
                project_id="0123456789abcdef",
                journal=MappingFailureJournal(Path(tmp) / "delivery.sqlite3"),
                client=FakeClient(), mode="shadow",
            )
            result = provider.record(event("task.started"))
            self.assertEqual(result["ambiguous"], 1)
            self.assertEqual(provider.journal.counts()["ambiguous"], 1)

    def test_nonretryable_unavailable_before_delivery_remains_pending(self):
        from orchestration.memos_bridge import MemOSUnavailableError

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient({
                "feedback.submit": MemOSUnavailableError("not started", ambiguous=False),
            })
            provider = self.provider(tmp, client)
            result = provider.record(event("feedback.recorded", payload={
                "polarity": "positive", "magnitude": 1,
            }))
            self.assertEqual(result["ambiguous"], 0)
            self.assertEqual(result["dead"], 0)
            self.assertEqual(provider.journal.counts()["pending"], 1)

    def test_heavy_methods_receive_upstream_75_second_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            provider = self.provider(tmp, client)
            provider.record(event("task.started"))
            provider.record(event("retrieval.used", suffix="2"))
            provider.record(event("task.completed", payload={"outcome_summary": "done"}))
            provider.record(event("feedback.recorded", suffix="3", payload={
                "polarity": "positive", "magnitude": 1,
            }))
            budgets = {}
            for method, timeout in client.timeouts:
                budgets.setdefault(method, set()).add(timeout)
            for method in ("turn.start", "turn.end", "memory.search", "feedback.submit"):
                self.assertEqual(budgets[method], {75.0})
            self.assertEqual(budgets["session.open"], {None})
            for method in ("episode.close", "session.close"):
                self.assertEqual(budgets[method], {15.0})
            self.assertEqual(budgets["core.health"], {75.0})

    def test_cold_start_health_uses_long_budget_and_warm_health_is_cached(self):
        class DelayedHealth(FakeClient):
            def health(self, *, timeout=None):
                time.sleep(0.05)
                return super().health(timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            client = DelayedHealth()
            provider = self.provider(tmp, client)
            self.assertEqual(provider.health()["status"], "healthy")
            self.assertEqual(provider.health()["status"], "healthy")
            health_calls = [item for item in client.timeouts if item[0] == "core.health"]
            self.assertEqual(health_calls, [("core.health", 75.0)])

    def test_unavailable_provider_degrades_without_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = self.provider(tmp, client=None)
            items, health = provider.retrieve("anything")
            self.assertEqual(items, [])
            self.assertEqual(health["status"], "degraded")
            self.assertIn("behavioral_unavailable", health["warnings"])

    def test_unavailable_completion_is_deferred_until_start_is_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = self.provider(tmp, client=None)
            provider.record(event("task.started"))
            provider.record(event("task.completed", payload={"outcome_summary": "done"}))
            self.assertIsNotNone(provider.journal.deferred_completion("run-1"))
            self.assertEqual(provider.health()["queue"]["deferred"], 1)
            provider.client = FakeClient()
            result = provider._drain()
            self.assertEqual(result["delivered"], 5)
            self.assertIsNone(provider.journal.deferred_completion("run-1"))
            self.assertEqual(provider.journal.counts()["pending"], 0)

    def test_wrong_version_never_receives_behavioral_events(self):
        class WrongVersion(FakeClient):
            def health(self):
                raise RuntimeError("wrong pinned version")

        with tempfile.TemporaryDirectory() as tmp:
            client = WrongVersion()
            provider = self.provider(tmp, client)
            result = provider.record(event("task.started"))
            self.assertEqual(result["delivered"], 0)
            self.assertEqual(client.calls, [])
            self.assertEqual(provider.journal.counts()["pending"], 2)

    def test_decision_and_recovery_retrieval_are_exercised_but_not_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            provider = self.provider(tmp, client)
            provider.record(event("task.started"))
            for index, reason in enumerate(("decision_point", "recovery"), start=2):
                provider.record(event(
                    "retrieval.used", suffix=str(index), payload={"reason": reason},
                ))
            searches = [call for call in client.calls if call[0] == "memory.search"]
            observations = [call for call in client.calls if call[0] == "feedback.submit"]
            self.assertEqual(len(searches), 2)
            self.assertEqual(
                [call[1]["filters"]["reason"] for call in searches],
                ["decision_point", "recovery"],
            )
            self.assertTrue(all(call[1]["topK"] == {
                "tier1": 5, "tier2": 5, "tier3": 5,
            } for call in searches))
            self.assertTrue(all(call[2] for call in searches))
            self.assertTrue(all(call[1]["channel"] == "implicit" for call in observations))
            self.assertEqual(provider.retrieve("decision")[0], [])

    def test_shadow_packet_keeps_governance_when_behavioral_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            behavioral = self.provider(tmp, client=None)
            governance = GovernanceProvider(AGENT, "0123456789abcdef", word_set)
            packet = build_shadow_packet(governance, behavioral, "permissions")
            self.assertTrue(packet.routing["governance"])
            self.assertTrue(packet.routing["behavioral"])
            self.assertTrue(packet.sections[0]["items"])
            self.assertEqual(packet.sections[1]["items"], ())
            self.assertEqual(packet.health["behavioral"]["status"], "degraded")
            self.assertIn("behavioral_unavailable", packet.warnings)

    def test_shadow_export_is_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            provider = self.provider(tmp, client)
            provider.journal.begin_run("run", "session")
            provider.journal.set_episode("run", "episode")
            exported = provider.export_shadow(limit=5, max_bytes=4096)
            self.assertEqual(exported["mode"], "shadow")
            self.assertEqual(exported["traces"][0]["summary"], "safe trace")
            self.assertLessEqual(len(json.dumps(exported).encode()), 4096)


if __name__ == "__main__":
    unittest.main()
