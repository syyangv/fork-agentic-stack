import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / ".agent"
sys.path.insert(0, str(AGENT / "memory"))

from orchestration.assist_gate import AssistQualityGate  # noqa: E402
from orchestration._core import SchemaValidationError, validate_schema  # noqa: E402
from orchestration.contracts import EventEnvelope, ProvenanceRef, RetrievalItem  # noqa: E402
from orchestration.fusion import fuse_retrieval  # noqa: E402
from orchestration.governance_fts import rank_governance_records  # noqa: E402
from orchestration.memos_journal import MemosDeliveryJournal  # noqa: E402
from orchestration.memos_factory import create_memos_provider  # noqa: E402
from orchestration.orchestrator import build_assist_packet  # noqa: E402
from orchestration.providers.memos_local import MemosLocalProvider  # noqa: E402
from orchestration.router import allocate_lane_budgets, route_intent  # noqa: E402


PROJECT = "0123456789abcdef"


def quality_metrics(**changes):
    value = {
        "schema": "agentic.memory.assist-quality.v1", "project_id": PROJECT,
        "measured_at": "2026-07-18T06:00:00Z",
        "source": {"evaluation_set_sha256": "sha256:" + "c" * 64,
                   "evaluator": "phase6-test"},
        "completed_episodes": 50, "task_categories": 5,
        "duplicate_rate": 0.01, "evaluation_queries": 30,
        "precision_at_5": 0.70, "cross_project_leaks": 0,
        "p95_recall_ms": 500,
    }
    value.update(changes)
    return value


class FakeClient:
    def __init__(self, result=None, failure=None, skills=None):
        self.result = result or {"hits": []}
        self.failure = failure
        self.skills = skills or []
        self.calls = []

    def health(self, *, timeout=None):
        return {"ok": True, "version": "2.0.10", "capabilities": ("memory.search",)}

    def call(self, method, params, *, timeout=None, retryable=False):
        self.calls.append((method, params, timeout, retryable))
        if self.failure and method == "memory.search":
            raise self.failure
        if method == "memory.search":
            return self.result
        if method == "skill.list":
            return {"skills": self.skills}
        if method == "turn.start":
            return {"query": {"episodeId": "episode-1"}, "hits": []}
        return {"ok": True}


def item(lane, item_id, summary, *, status="active", score=0.8, tokens=10):
    kind = {
        "governance": "lesson", "behavioral": "skill", "evidence": "crg_node",
    }[lane]
    provider = {
        "governance": "agentic-stack", "behavioral": "memos-local", "evidence": "crg",
    }[lane]
    provenance = ProvenanceRef(
        kind=kind, provider=provider, source_id=item_id, project_id=PROJECT,
        repository_revision="a" * 40 if lane == "evidence" else None,
        source_hash="sha256:" + "b" * 64,
        observed_at="2026-07-18T06:00:00Z", confidence=score,
        freshness="stale" if status == "stale" else "fresh", locator={},
    )
    return RetrievalItem(
        item_id=item_id, lane=lane, type=kind, summary=summary,
        scope={"project_id": PROJECT, "harness": None}, status=status,
        provider_score=score, selection_reason=f"{lane} candidate",
        provenance=(provenance.to_dict(),), token_estimate=tokens, expires_at=None,
    )


def event(event_type, *, suffix, payload=None):
    return EventEnvelope.create(
        timestamp="2026-07-18T06:00:00Z", event_type=event_type,
        project_id=PROJECT, repo_root="/repo", revision="a" * 40,
        harness="codex", run_id="run-1", session_id="session-1",
        actor="tool" if event_type == "tool.completed" else "agent",
        intent="recover index build", payload=payload or {},
        idempotency_key=f"phase6-{event_type}-{suffix}",
    )


class AssistGateTest(unittest.TestCase):
    def test_manifest_declares_phase6_without_enabling_unmet_assist(self):
        manifest = json.loads((AGENT / "infrastructure.json").read_text())
        config = json.loads((AGENT / "memory/orchestration/config.json").read_text())
        self.assertGreaterEqual(manifest["orchestration_phase"], 6)
        self.assertTrue({
            "assist_quality_gate", "bounded_behavioral_retrieval",
            "authority_ordered_fusion", "retrieval_preview",
            "lifecycle_retrieval_observations", "local_governance_fts",
        }.issubset(manifest["features"]))
        self.assertEqual(config["mode"], "off")

    def test_missing_or_incomplete_metrics_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = AssistQualityGate.from_path(Path(tmp) / "missing.json")
            self.assertFalse(missing.eligible)
            self.assertIn("assist_metrics_missing", missing.warnings)

            path = Path(tmp) / "metrics.json"
            path.write_text(json.dumps(quality_metrics(completed_episodes=49)))
            gate = AssistQualityGate.from_path(path, project_id=PROJECT)
            self.assertFalse(gate.eligible)
            self.assertIn("assist_gate_episodes", gate.warnings)

    def test_exact_documented_thresholds_are_eligible(self):
        gate = AssistQualityGate.from_mapping(quality_metrics(
            duplicate_rate=0.049, p95_recall_ms=749,
        ), project_id=PROJECT)
        self.assertTrue(gate.eligible)
        self.assertEqual(gate.warnings, ())

    def test_non_finite_metrics_fail_closed(self):
        baseline = quality_metrics()
        for key, value in (("completed_episodes", float("inf")),
                           ("precision_at_5", float("nan")),
                           ("p95_recall_ms", float("-inf"))):
            metrics = {**baseline, key: value}
            with self.subTest(key=key):
                self.assertFalse(AssistQualityGate.from_mapping(metrics).eligible)

    def test_metrics_schema_ranges_and_project_binding_fail_closed(self):
        for changes in (
            {"completed_episodes": 50.5}, {"precision_at_5": 999},
            {"unexpected": True}, {"project_id": "fedcba9876543210"},
        ):
            with self.subTest(changes=changes):
                gate = AssistQualityGate.from_mapping(
                    quality_metrics(**changes), project_id=PROJECT,
                )
                self.assertFalse(gate.eligible)

    def test_governance_fts_ranks_relevant_authority_locally(self):
        scores = rank_governance_records("database migration rollback", [
            ("relevant", "Always rehearse database migration rollback."),
            ("unrelated", "Prefer concise communication."),
        ])
        self.assertGreater(scores["relevant"], scores["unrelated"])


class BehavioralAssistTest(unittest.TestCase):
    def provider(self, tmp, client):
        return MemosLocalProvider(
            project_id=PROJECT,
            journal=MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3"),
            client=client, mode="assist",
        )

    def test_assist_translates_bounded_hits_with_support_gain_and_evidence(self):
        result = {"hits": [{
            "tier": "tier1", "refId": "skill-1", "refKind": "skill",
            "snippet": "Retry the index build after validating file hashes.",
            "score": 0.91, "ownerAgentKind": "hermes",
            "ownerProfileId": PROJECT, "ownerWorkspaceId": PROJECT,
        }]}
        skills = [{
            "id": "skill-1", "name": "index repair", "status": "active",
            "invocationGuide": "Retry safely", "support": 4, "gain": 0.35,
            "evidenceAnchors": ["evi_" + "a" * 64],
            "sourcePolicyIds": [], "sourceWorldModelIds": [],
            "ownerAgentKind": "hermes", "ownerProfileId": PROJECT,
            "ownerWorkspaceId": PROJECT, "updatedAt": 1784354400000,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(result, skills=skills)
            items, health = self.provider(tmp, client).retrieve(
                "recover index build", top_k=5, reason="recovery", run_id="run-1",
            )
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(len(items), 1)
        translated = items[0]
        self.assertEqual(translated.lane, "behavioral")
        self.assertEqual(translated.type, "skill")
        self.assertIn("support=4", translated.selection_reason)
        self.assertIn("gain=0.350", translated.selection_reason)
        self.assertEqual(
            translated.to_dict()["provenance"][0]["locator"]["evidence_refs"],
            ["evi_" + "a" * 64],
        )
        search = client.calls[0]
        self.assertEqual(search[0], "memory.search")
        self.assertEqual(search[1]["agent"], "hermes")
        self.assertLessEqual(search[2], 0.75)
        self.assertTrue(search[3])
        self.assertEqual(client.calls[1][0], "skill.list")

    def test_signed_negative_gain_is_preserved(self):
        result = {"hits": [{
            "tier": "tier1", "refId": "skill-1", "refKind": "skill",
            "snippet": "Avoid the failed repair.", "score": 0.8,
            "ownerProfileId": PROJECT, "ownerWorkspaceId": PROJECT,
        }]}
        skills = [{
            "id": "skill-1", "status": "active", "support": 2, "gain": -0.25,
            "ownerAgentKind": "hermes", "ownerProfileId": PROJECT,
            "ownerWorkspaceId": PROJECT,
            "updatedAt": 1784354400000,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            items, _health = self.provider(tmp, FakeClient(result, skills=skills)).retrieve(
                "repair", run_id="run-1",
            )
        self.assertEqual(items[0].provenance[0]["locator"]["gain"], -0.25)
        self.assertIn("gain=-0.250", items[0].selection_reason)

    def test_cross_project_or_sensitive_hits_are_rejected_not_injected(self):
        result = {"hits": [
            {"tier": "tier2", "refId": "foreign", "refKind": "trace",
             "snippet": "safe", "ownerProfileId": "f" * 16,
             "ownerWorkspaceId": "f" * 16, "score": 1},
            {"tier": "tier2", "refId": "secret", "refKind": "trace",
             "snippet": "Bearer abcdefghijklmnop", "ownerProfileId": PROJECT,
             "ownerWorkspaceId": PROJECT, "score": 1},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            items, health = self.provider(tmp, FakeClient(result)).retrieve("auth")
        self.assertEqual(items, [])
        self.assertIn("behavioral_cross_project_hit", health["warnings"])
        self.assertIn("behavioral_sensitive_hit", health["warnings"])

    def test_unowned_and_hub_shared_hits_fail_closed(self):
        result = {"hits": [
            {"tier": "tier1", "refId": "unowned", "refKind": "skill",
             "snippet": "not attributable", "score": 1},
            {"tier": "tier1", "refId": "shared", "refKind": "skill",
             "snippet": "shared guidance", "score": 1, "shareScope": "hub",
             "ownerProfileId": PROJECT, "ownerWorkspaceId": PROJECT},
        ]}
        skills = [{"id": "unowned", "status": "active"}]
        with tempfile.TemporaryDirectory() as tmp:
            items, health = self.provider(tmp, FakeClient(result, skills=skills)).retrieve(
                "repair",
            )
        self.assertEqual(items, [])
        self.assertIn("behavioral_unowned_or_cross_project_hit", health["warnings"])
        self.assertIn("behavioral_shared_hit_rejected", health["warnings"])

    def test_detail_only_hub_share_fails_closed(self):
        result = {"hits": [{
            "tier": "tier1", "refId": "shared", "refKind": "skill",
            "snippet": "thin raw hit", "score": 1,
        }]}
        skills = [{
            "id": "shared", "status": "active", "share": {"scope": "hub"},
            "ownerAgentKind": "hermes", "ownerProfileId": PROJECT,
            "ownerWorkspaceId": PROJECT,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            items, health = self.provider(tmp, FakeClient(result, skills=skills)).retrieve(
                "repair",
            )
        self.assertEqual(items, [])
        self.assertIn("behavioral_shared_hit_rejected", health["warnings"])

    def test_policy_world_trace_and_episode_use_read_only_enrichment_methods(self):
        class EnrichmentClient(FakeClient):
            def call(self, method, params, *, timeout=None, retryable=False):
                self.calls.append((method, params, timeout, retryable))
                if method == "memory.search":
                    return {"hits": [
                        {"tier": "tier2", "refId": "policy-1", "refKind": "experience",
                         "snippet": "policy", "score": .8},
                        {"tier": "tier3", "refId": "world-1", "refKind": "world-model",
                         "snippet": "world", "score": .7},
                        {"tier": "tier2", "refId": "trace-1", "refKind": "trace",
                         "snippet": "trace", "score": .6},
                        {"tier": "tier2", "refId": "episode-1", "refKind": "episode",
                         "snippet": "episode", "score": .5},
                    ]}
                detail = {
                    "ownerAgentKind": "hermes", "ownerProfileId": PROJECT,
                    "ownerWorkspaceId": PROJECT,
                    "status": "active", "updatedAt": 1784354400000,
                }
                if method == "memory.timeline":
                    return {"traces": [{**detail, "episodeId": params["episodeId"]}]}
                return detail

        with tempfile.TemporaryDirectory() as tmp:
            client = EnrichmentClient()
            items, health = self.provider(tmp, client).retrieve("repair", top_k=4)
        self.assertEqual(health["status"], "healthy")
        self.assertEqual([row.type for row in items], [
            "policy", "world_model", "trace", "trace",
        ])
        methods = [call[0] for call in client.calls]
        self.assertEqual(methods, [
            "memory.search", "memory.get_policy", "memory.get_world",
            "memory.get_trace", "memory.timeline",
        ])
        self.assertNotIn("skill.get", methods)

    def test_timeout_is_task_visible_and_non_blocking(self):
        from orchestration.memos_bridge import MemOSTimeoutError
        with tempfile.TemporaryDirectory() as tmp:
            items, health = self.provider(
                tmp, FakeClient(failure=MemOSTimeoutError("slow")),
            ).retrieve("anything")
        self.assertEqual(items, [])
        self.assertEqual(health["status"], "degraded")
        self.assertIn("behavioral_retrieval_error:MemOSTimeoutError", health["warnings"])

    def test_project_lock_contention_respects_assist_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = self.provider(tmp, FakeClient())
            entered = threading.Event()
            release = threading.Event()

            def hold_lock():
                with provider.journal.delivery_worker():
                    entered.set()
                    release.wait(2)

            thread = threading.Thread(target=hold_lock)
            thread.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            try:
                items, health = provider.retrieve("repair", run_id="run-1")
            finally:
                release.set()
                thread.join(2)
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertEqual(items, [])
            self.assertIn("behavioral_retrieval_error:TimeoutError", health["warnings"])

    def test_assist_session_construction_and_close_share_one_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = create_memos_provider(
                AGENT, PROJECT, mode="shadow", code_root=root / "code",
                data_root=root / "state",
            )
            entered = threading.Event()
            release = threading.Event()

            def hold_session():
                with shadow:
                    entered.set()
                    release.wait(2)

            thread = threading.Thread(target=hold_session)
            thread.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            deadline = started + 0.5
            try:
                session = create_memos_provider(
                    AGENT, PROJECT, mode="assist", code_root=root / "code",
                    data_root=root / "state", assist_deadline=deadline,
                )
                with session as provider:
                    items, health = provider.retrieve("repair")
                elapsed = time.monotonic() - started
            finally:
                release.set()
                thread.join(2)
            self.assertLess(elapsed, 0.7)
            self.assertEqual(items, [])
            self.assertIn("behavioral_project_lock_timeout", health["warnings"])

    def test_lifecycle_records_start_recovery_decision_and_final_outcomes(self):
        search = {"hits": [{
            "tier": "tier1", "refId": "skill-1", "refKind": "skill",
            "snippet": "Retry safely", "score": .8,
            "ownerProfileId": PROJECT, "ownerWorkspaceId": PROJECT,
        }]}
        skills = [{
            "id": "skill-1", "status": "active", "support": 2, "gain": -.2,
            "evidenceAnchors": ["trace-1"], "sourcePolicyIds": [],
            "sourceWorldModelIds": [], "ownerAgentKind": "hermes",
            "ownerProfileId": PROJECT,
            "ownerWorkspaceId": PROJECT, "updatedAt": 1784354400000,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(search, skills=skills)
            provider = self.provider(tmp, client)
            provider.record(event("task.started", suffix="1"))
            provider.record_injected(
                "run-1", ["memos:skill-1"], reason="task_start",
            )
            provider.record(event("tool.completed", suffix="2", payload={
                "tool_name": "pytest", "error_code": "failed",
            }))
            provider.record(event("tool.completed", suffix="3", payload={
                "tool_name": "pytest", "error_code": "failed-again",
            }))
            provider.record(event("retrieval.used", suffix="4", payload={
                "reason": "decision_point", "item_ids": ["memos:skill-1"],
                "outcome": "contradicted",
            }))
            provider.record_injected(
                "run-1", ["memos:skill-1"], reason="decision_point",
            )
            provider.journal.mark_retrievals(
                "run-1", ["memos:skill-1"], "contradicted",
                reason="decision_point",
            )
            provider.record(event("task.completed", suffix="5", payload={
                "outcome_summary": "done", "status": "verified",
                "verification_evidence": ["evi_" + "b" * 64],
            }))
            rows = provider.journal.retrievals_for_run("run-1")
            invocation_reasons = provider.journal.retrieval_reasons_for_run("run-1")
        reasons = [
            call[1]["filters"]["reason"] for call in client.calls
            if call[0] == "memory.search"
        ]
        self.assertEqual(reasons.count("task_start"), 1)
        self.assertEqual(reasons.count("recovery"), 1)
        self.assertEqual(reasons.count("decision_point"), 1)
        self.assertEqual(
            set(invocation_reasons), {"task_start", "recovery", "decision_point"},
        )
        self.assertEqual({(row["reason"], row["outcome"]) for row in rows}, {
            ("task_start", "ignored"), ("decision_point", "contradicted"),
        })
        turn_end = next(call for call in client.calls if call[0] == "turn.end")
        hints = turn_end[1]["contextHints"]
        self.assertEqual(hints["outcomeClass"], "verified")
        self.assertEqual(hints["verificationEvidence"], ["evi_" + "b" * 64])
        self.assertEqual(len(hints["retrievalOutcomes"]), 2)


class FusionTest(unittest.TestCase):
    def test_fusion_records_only_selected_injected_items(self):
        class Provider:
            def __init__(self, lane, rows):
                self.project_id = PROJECT
                self.lane = lane
                self.rows = rows
                self.recorded = None

            def retrieve(self, *_args, **_kwargs):
                return self.rows, {"status": "healthy", "warnings": []}

            def record_injected(self, run_id, item_ids, *, reason):
                self.recorded = (run_id, item_ids, reason)

        governance = Provider("governance", [item(
            "governance", "gov", "authority",
        )])
        behavioral = Provider("behavioral", [
            item("behavioral", "kept", "repair", tokens=10),
            item("behavioral", "large", "oversized", tokens=1200),
        ])
        evidence = Provider("evidence", [item(
            "evidence", "fresh", "code evidence",
        )])
        _packet, preview = build_assist_packet(
            governance, behavioral, evidence, "debug repository failure",
            total_budget=100, lane_reserves={
                "governance": 40, "behavioral": 30, "evidence": 30,
            }, run_id="run-1", reason="recovery",
        )
        self.assertEqual(
            behavioral.recorded,
            ("run-1", ["gov", "kept", "fresh"], "recovery"),
        )
        self.assertEqual([row["item_id"] for row in preview["over_budget"]], ["large"])

    def test_governance_survives_behavioral_and_evidence_outages(self):
        class Governance:
            project_id = PROJECT

            def retrieve(self, _intent, top_k=5):
                return [item("governance", "gov", "authoritative permission")], {
                    "status": "healthy", "warnings": [],
                }

        class Broken:
            def retrieve(self, *_args, **_kwargs):
                raise TimeoutError("provider unavailable")

        packet, preview = build_assist_packet(
            Governance(), Broken(), Broken(), "debug repository failure",
            total_budget=100,
            lane_reserves={"governance": 40, "behavioral": 30, "evidence": 30},
        )
        self.assertEqual(packet.sections[0]["items"][0]["item_id"], "gov")
        self.assertEqual(packet.sections[1]["items"], ())
        self.assertEqual(packet.sections[2]["items"], ())
        self.assertIn("behavioral_retrieval_error:TimeoutError", packet.warnings)
        self.assertIn("evidence_retrieval_error:TimeoutError", packet.warnings)
        self.assertEqual(preview["selected"][0]["item_id"], "gov")

    def test_authority_order_dedup_stale_and_budget_preview(self):
        governance = [item("governance", "gov", "same guidance", score=1, tokens=10)]
        behavioral = [
            item("behavioral", "dup", "same guidance", score=.9, tokens=10),
            item("behavioral", "kept", "behavioral repair", score=.8, tokens=15),
            item("behavioral", "large", "too large", score=.7, tokens=90),
        ]
        evidence = [
            item("evidence", "stale", "old code", status="stale", tokens=10),
            item("evidence", "fresh", "current code", status="fresh", tokens=10),
        ]
        route = route_intent("debug repository test failure")
        packet, preview = fuse_retrieval(
            intent="debug repository test failure", project_id=PROJECT, route=route,
            items={"governance": governance, "behavioral": behavioral,
                   "evidence": evidence},
            health={"governance": {"status": "healthy"},
                    "behavioral": {"status": "healthy"},
                    "evidence": {"status": "healthy"}},
            budgets={"governance": 20, "behavioral": 20, "evidence": 20},
        )
        self.assertEqual([section["lane"] for section in packet.sections], [
            "governance", "behavioral", "evidence",
        ])
        self.assertEqual([row["item_id"] for row in packet.sections[0]["items"]], ["gov"])
        self.assertEqual([row["item_id"] for row in packet.sections[1]["items"]], ["kept"])
        self.assertEqual([row["item_id"] for row in packet.sections[2]["items"]], ["fresh"])
        self.assertEqual([row["item_id"] for row in preview["deduplicated"]], ["dup"])
        self.assertEqual([row["item_id"] for row in preview["stale"]], ["stale"])
        self.assertEqual([row["item_id"] for row in preview["over_budget"]], ["large"])
        self.assertLessEqual(packet.token_estimate, 60)

    def test_router_allocations_remain_bounded(self):
        budgets = allocate_lane_budgets(route_intent("debug repository failure"))
        self.assertEqual(sum(budgets.values()), 12_000)

    def test_preview_descriptors_are_bounded_and_report_truncation(self):
        rows = [item(
            "behavioral", f"large-{index}", f"oversized {index}", tokens=1200,
        ) for index in range(250)]
        _packet, preview = fuse_retrieval(
            intent="debug repository failure", project_id=PROJECT,
            route=route_intent("debug repository failure"),
            items={"governance": [], "behavioral": rows, "evidence": []},
            health={lane: {"status": "healthy"} for lane in (
                "governance", "behavioral", "evidence",
            )},
            budgets={"governance": 0, "behavioral": 0, "evidence": 0},
        )
        self.assertEqual(len(preview["over_budget"]), 100)
        self.assertEqual(preview["category_counts"]["over_budget"], 250)
        self.assertIn("over_budget", preview["truncated"])

    def test_preview_schema_rejects_wrong_types_duplicates_and_overflow(self):
        valid = {
            "schema": "agentic.memory.retrieval-preview.v1",
            "selected": [], "rejected": [], "deduplicated": [], "stale": [],
            "over_budget": [], "category_counts": {
                "selected": 0, "rejected": 0, "deduplicated": 0,
                "stale": 0, "over_budget": 0,
            }, "truncated": [], "token_estimate": 0,
            "lane_budgets": {"governance": 0, "behavioral": 0, "evidence": 0},
        }
        invalid = [
            {**valid, "selected": "not-an-array"},
            {**valid, "truncated": ["selected", "selected"]},
            {**valid, "selected": [
                {"lane": "governance", "item_id": f"id-{index}", "reason": "x"}
                for index in range(101)
            ]},
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(SchemaValidationError):
                    validate_schema(value, "retrieval-preview-v1.schema.json")


if __name__ == "__main__":
    unittest.main()
