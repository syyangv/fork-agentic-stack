import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / ".agent"
sys.path.insert(0, str(AGENT / "memory"))

from orchestration.promotion import (  # noqa: E402
    PromotionError, stage_behavioral_candidates, translate_memos_record,
)
from orchestration.revalidation import (  # noqa: E402
    EvidenceValidationError, RevalidationIndex, apply_outcome, revalidate_lessons,
    record_retrieval_outcome, validate_candidate_evidence,
    validate_live_candidate_evidence,
)
from orchestration._core import SchemaValidationError, validate_schema  # noqa: E402
from orchestration.memos_journal import MemosDeliveryJournal  # noqa: E402
from orchestration.providers.memos_local import MemosLocalProvider  # noqa: E402
from orchestration.governance_recall import recall_lessons  # noqa: E402
from render_lessons import render_lessons  # noqa: E402


PROJECT = "0123456789abcdef"
REVISION = "a" * 40


def load_tool(name):
    path = AGENT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"phase7_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def owned(**values):
    return {
        "ownerAgentKind": "hermes", "ownerProfileId": PROJECT,
        "ownerWorkspaceId": PROJECT,
        "status": "active", "updatedAt": 1784354400000, **values,
    }


def evidence(evidence_id, kind, *, revision=REVISION, executed=False, exit_code=0):
    provider = "test-runner" if kind == "test_run" else "crg"
    locator = (
        {"executed_test": True, "exit_code": exit_code, "test_ids": ["test_ok"],
         "command_digest": "sha256:" + "d" * 64}
        if kind == "test_run" else
        {"graph_updated_at": "2026-07-18T06:00:00Z", "symbols": [
            {"qualified_name": "pkg.fn", "file_path": "pkg.py", "file_hash": "sha256:" + "b" * 64},
        ], "relationships": ["TESTED_BY"], "working_tree": False}
    )
    return {
        "schema": "agentic.memory.evidence-ledger.v1", "evidence_id": evidence_id,
        "summary": kind,
        "provenance": {
            "kind": kind, "provider": provider, "source_id": evidence_id,
            "project_id": PROJECT, "repository_revision": revision,
            "source_hash": "sha256:" + "c" * 64,
            "observed_at": "2026-07-18T06:00:00Z", "confidence": 1,
            "freshness": "fresh", "locator": locator,
        },
        "verification": {
            "repository_reconciled": True,
            "files_reconciled": kind != "test_run",
            "symbols_reconciled": kind != "test_run",
            "executed_test": executed,
        },
    }


class PromotionTranslationTest(unittest.TestCase):
    def test_provider_discovery_preserves_bounded_policy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = MemosLocalProvider(
                project_id=PROJECT,
                journal=MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3"),
                client=None, mode="assist",
            )
            item = provider._translate_hit(
                {"refId": "policy-detail", "refKind": "policy",
                 "snippet": "Retry policy", "score": 0.9, "tier": "tier1"},
                owned(
                    id="policy-detail", title="Retry safely",
                    trigger="Only after an index failure.",
                    procedure="Validate current hashes before retrying.",
                    verification="Run the focused test suite.",
                    boundary="Never cross project scope.", support=3, gain=.2,
                ),
                "policy",
            )
            with mock.patch.object(provider, "retrieve", return_value=(
                [item], {"status": "healthy", "warnings": []},
            )):
                candidates, _health = provider.discover_candidates("retry")
            self.assertEqual(len(candidates), 1)
            self.assertIn("Only after an index failure", candidates[0]["claim"])
            self.assertIn("Validate current hashes", candidates[0]["claim"])

    def test_manifest_declares_phase7_without_auto_accept_feature(self):
        manifest = json.loads((AGENT / "infrastructure.json").read_text())
        self.assertEqual(manifest["orchestration_phase"], 7)
        self.assertTrue({
            "behavioral_candidate_translation", "human_only_candidate_promotion",
            "evidence_bound_graduation", "support_contradiction_updates",
            "crg_linked_revalidation", "behavioral_stale_overrides",
            "unified_memory_review",
        }.issubset(manifest["features"]))
        self.assertNotIn("automatic_acceptance", manifest["features"])

    def test_policy_skill_world_and_repair_are_staged_never_accepted(self):
        records = [
            ("policy", owned(
                id="policy-1", title="Retry safely", trigger="index failure",
                procedure="Validate hashes, then retry.", verification="Run tests.",
                boundary="Same project only.", support=4, gain=-0.2,
                experienceType="failure_avoidance", sourceTraceIds=["trace-1"],
            )),
            ("skill", owned(
                id="skill-1", name="Index repair", invocationGuide="Repair the index.",
                support=5, gain=0.4, trialsAttempted=6, trialsPassed=5,
                evidenceAnchors=["evi_" + "1" * 64],
            )),
            ("world_model", owned(
                id="world-1", title="Index topology", body="Indexes are per project.",
                structure={"constraints": [{"label": "scope", "description": "project", "evidenceIds": ["trace-2"]}]},
                policyIds=["policy-1"],
            )),
            ("decision_repair", owned(
                id="repair-1", preference="Validate before retrying.",
                antiPattern="Blind retries", highValueTraceIds=["trace-3"],
                lowValueTraceIds=["trace-4"], validated=False,
                captureSource="normalized_event",
            )),
        ]
        translated = [translate_memos_record(kind, row, PROJECT) for kind, row in records]
        self.assertEqual([row["source_kind"] for row in translated], [
            "policy", "skill", "world_model", "decision_repair",
        ])
        self.assertTrue(all(row["status"] == "staged" for row in translated))
        self.assertEqual(translated[0]["gain"], -0.2)
        self.assertEqual(translated[1]["trial_count"], 6)
        self.assertEqual(translated[1]["trial_pass_count"], 5)
        self.assertIsNone(translated[2]["support"])
        self.assertNotIn("accepted_at", translated[3])

    def test_repair_policy_stays_policy_without_private_db_access(self):
        candidate = translate_memos_record("policy", owned(
            id="policy-repair", title="Repair", procedure="Prefer verified path.",
            experienceType="repair_validated", preference=["verified path"],
            antiPattern=["blind change"], support=2, gain=0.1,
        ), PROJECT)
        self.assertEqual(candidate["source_kind"], "policy")
        self.assertEqual(candidate["provider_ids"]["policy_id"], "policy-repair")

    def test_foreign_unowned_and_hub_records_fail_closed(self):
        rows = [
            owned(id="x", name="x", ownerProfileId="f" * 16),
            {"id": "x", "name": "x", "ownerProfileId": PROJECT},
            owned(id="x", name="x", share={"scope": "hub"}),
        ]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(PromotionError):
                translate_memos_record("skill", row, PROJECT)

    def test_staging_preserves_human_rejection_until_explicit_reopen(self):
        candidate = translate_memos_record("skill", owned(
            id="skill-terminal", name="Terminal rejection",
            invocationGuide="Do not silently reopen.", support=1, gain=0,
        ), PROJECT)
        with tempfile.TemporaryDirectory() as tmp:
            candidates = Path(tmp) / "candidates"
            candidates.mkdir()
            rejected = candidates / "rejected"
            rejected.mkdir()
            terminal = {**candidate, "status": "rejected", "decisions": [{
                "action": "rejected", "reviewer": "human", "ts": "2026-07-18T00:00:00Z",
            }]}
            (rejected / f"{candidate['id']}.json").write_text(json.dumps(terminal))
            self.assertEqual(stage_behavioral_candidates([candidate], candidates), 0)
            self.assertTrue((rejected / f"{candidate['id']}.json").exists())

    def test_machine_rejection_restages_to_exactly_one_lifecycle_file(self):
        candidate = translate_memos_record("skill", owned(
            id="skill-machine-reject", name="Machine rejected guidance",
            invocationGuide="Restage this item for explicit human review.",
            support=1, gain=0,
        ), PROJECT)
        with tempfile.TemporaryDirectory() as tmp:
            candidates = Path(tmp) / "candidates"
            rejected = candidates / "rejected"
            rejected.mkdir(parents=True)
            terminal = {**candidate, "status": "rejected", "decisions": [{
                "action": "rejected", "reviewer": "heuristic_prefilter",
                "ts": "2026-07-18T00:00:00Z",
            }]}
            (rejected / f"{candidate['id']}.json").write_text(json.dumps(terminal))
            self.assertEqual(stage_behavioral_candidates([candidate], candidates), 1)
            matches = list(candidates.rglob(f"{candidate['id']}.json"))
            self.assertEqual(matches, [candidates / f"{candidate['id']}.json"])

    def test_duplicate_lifecycle_locations_fail_closed(self):
        candidate = translate_memos_record("skill", owned(
            id="skill-duplicate", name="Duplicate lifecycle guidance",
            invocationGuide="Refuse ambiguous lifecycle state until repaired.",
        ), PROJECT)
        with tempfile.TemporaryDirectory() as tmp:
            candidates = Path(tmp) / "candidates"; deferred = candidates / "deferred"
            deferred.mkdir(parents=True)
            for path in (candidates / f"{candidate['id']}.json",
                         deferred / f"{candidate['id']}.json"):
                path.write_text(json.dumps(candidate))
            with self.assertRaises(PromotionError):
                stage_behavioral_candidates([candidate], candidates)

    def test_candidate_schema_rejects_nested_ids_extra_decision_and_secret(self):
        candidate = translate_memos_record("policy", owned(
            id="policy-schema", title="Validate candidate schema",
            procedure="Reject nested identities and unexpected lifecycle fields.",
        ), PROJECT)
        invalid = [
            {**candidate, "provider_ids": {"policy_id": {"nested": "x"}}},
            {**candidate, "provider_ids": {}},
            {**candidate, "decisions": [{
                **candidate["decisions"][0], "unexpected": True,
            }]},
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(SchemaValidationError):
                validate_schema(row, "candidate-v1.schema.json")
        with self.assertRaises(PromotionError):
            translate_memos_record("policy", owned(
                id="policy-secret", title="Never expose credentials",
                procedure="Use token=plain-secret-value in the command.",
            ), PROJECT)


class EvidenceAndRevalidationTest(unittest.TestCase):
    def candidate(self):
        return {
            "id": "memos_skill_1", "project_scope": {"project_id": PROJECT},
            "code_specific": True,
            "evidence_refs": ["evi_" + "1" * 64, "evi_" + "2" * 64],
            "code_refs": [{"file_path": "pkg.py", "qualified_name": "pkg.fn"}],
        }

    def valid_rows(self):
        return [
            evidence("evi_" + "1" * 64, "crg_node"),
            evidence("evi_" + "2" * 64, "test_run", executed=True),
        ]

    def test_code_candidate_requires_fresh_crg_and_passing_executed_test(self):
        report = validate_candidate_evidence(
            self.candidate(), self.valid_rows(), project_id=PROJECT,
            revision=REVISION, graph_updated_at="2026-07-18T06:00:00Z",
        )
        self.assertTrue(report["eligible"])
        self.assertEqual(len(report["crg_evidence_ids"]), 1)
        self.assertEqual(len(report["test_evidence_ids"]), 1)

    def test_missing_stale_cross_project_and_failing_evidence_fail_closed(self):
        unreconciled_test = evidence(
            "evi_" + "2" * 64, "test_run", executed=True,
        )
        unreconciled_test["verification"]["repository_reconciled"] = False
        cases = [
            self.valid_rows()[:1],
            [self.valid_rows()[0], evidence("evi_" + "2" * 64, "test_run", executed=True, revision="d" * 40)],
            [self.valid_rows()[0], evidence("evi_" + "2" * 64, "test_run", executed=True, exit_code=1)],
            [{**self.valid_rows()[0], "provenance": {**self.valid_rows()[0]["provenance"], "project_id": "f" * 16}}, self.valid_rows()[1]],
            [self.valid_rows()[0], unreconciled_test],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(EvidenceValidationError):
                validate_candidate_evidence(
                    self.candidate(), rows, project_id=PROJECT,
                    revision=REVISION, graph_updated_at="2026-07-18T06:00:00Z",
                )

    def test_tested_by_relationship_is_not_executed_test(self):
        rows = [evidence("evi_" + "1" * 64, "crg_node")]
        candidate = {**self.candidate(), "evidence_refs": ["evi_" + "1" * 64]}
        with self.assertRaises(EvidenceValidationError):
            validate_candidate_evidence(
                candidate, rows, project_id=PROJECT, revision=REVISION,
                graph_updated_at="2026-07-18T06:00:00Z",
            )

    def test_outcomes_update_counts_without_changing_status(self):
        record = {"id": "x", "status": "accepted", "support_count": 1,
                  "contradiction_count": 0, "outcome_history": []}
        supported = apply_outcome(record, "used", "run-1")
        contradicted = apply_outcome(supported, "contradicted", "run-2")
        self.assertEqual(contradicted["support_count"], 2)
        self.assertEqual(contradicted["contradiction_count"], 1)
        self.assertEqual(contradicted["status"], "accepted")
        with self.assertRaises(ValueError):
            apply_outcome(contradicted, "used", "run-2")
        bounded = {**record, "outcome_history": [
            {"outcome_id": f"old-{index}", "outcome": "used",
             "observed_at": "2026-07-18T00:00:00Z"}
            for index in range(1000)
        ]}
        self.assertEqual(apply_outcome(bounded, "used", "new-event"), bounded)

    def test_linked_skill_becomes_locally_stale_without_deletion(self):
        candidate = translate_memos_record("skill", owned(
            id="skill-stale", name="Stale skill guidance",
            invocationGuide="Use only while evidence is current.",
            evidenceAnchors=["evi_" + "1" * 64], support=2, gain=.1,
        ), PROJECT)

        class Client:
            def health(self, *, timeout=None):
                return {"version": "2.0.10"}

            def call(self, method, params, *, timeout=None, retryable=False):
                if method == "memory.search":
                    return {"hits": [{"tier": "tier1", "refId": "skill-stale",
                                      "refKind": "skill", "snippet": candidate["claim"],
                                      "score": 1}]}
                if method == "skill.list":
                    return {"skills": [owned(
                        id="skill-stale", name="Stale skill guidance",
                        invocationGuide="Use only while evidence is current.",
                        evidenceAnchors=["evi_" + "1" * 64], support=2, gain=.1,
                    )]}
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp) / "memory"
            candidates = memory / "candidates"
            self.assertEqual(stage_behavioral_candidates([candidate], candidates), 1)
            path = candidates / f"{candidate['id']}.json"
            self.assertTrue(path.exists())
            index = RevalidationIndex(memory / "evidence/revalidation.sqlite3")
            affected = index.mark_evidence_stale(
                ["evi_" + "1" * 64], "revision drift", "event-1",
            )
            self.assertEqual(affected, [candidate["id"]])
            provider = MemosLocalProvider(
                project_id=PROJECT,
                journal=MemosDeliveryJournal(Path(tmp) / "delivery.sqlite3"),
                client=Client(), mode="assist", revalidation_index=index,
            )
            items, health = provider.retrieve("stale skill")
            self.assertEqual(items, [])
            self.assertIn("behavioral_revalidation_needed", health["warnings"])
            self.assertTrue(path.exists())

    def test_revision_drift_appends_revalidation_state_and_recall_excludes(self):
        lesson = {
            "id": "lesson_code", "claim": "Use verified index repair.",
            "conditions": ["index", "repair"], "status": "accepted",
            "accepted_at": "2026-07-18T06:00:00Z", "confidence": 1,
            "evidence_ids": ["evi_" + "1" * 64, "evi_" + "2" * 64],
            "project_scope": {"project_id": PROJECT}, "code_specific": True,
            "evidence_snapshot": {"repository_revision": REVISION},
        }
        with tempfile.TemporaryDirectory() as tmp:
            semantic = Path(tmp)
            (semantic / "lessons.jsonl").write_text(json.dumps(lesson) + "\n")
            (semantic / "LESSONS.md").write_text(
                "## Auto-promoted entries will be appended below\n\n- Use verified index repair.\n"
            )
            changed = revalidate_lessons(
                semantic, project_id=PROJECT, revision="d" * 40,
                evidence_rows=self.valid_rows(),
                graph_updated_at="2026-07-18T06:00:00Z",
            )
            self.assertEqual(changed, ["lesson_code"])
            rows = [json.loads(line) for line in (semantic / "lessons.jsonl").read_text().splitlines()]
            self.assertEqual(rows[-1]["status"], "revalidation_needed")
            self.assertEqual(rows[-1]["claim"], lesson["claim"])
            recalled, meta = recall_lessons(
                "verified index repair", semantic / "lessons.jsonl",
                semantic / "LESSONS.md", lambda value: set(value.lower().split()), top_k=5,
            )
            self.assertEqual(recalled, [])
            self.assertEqual(meta["considered"], 0)

            # A repeated revalidation sees the tombstone as latest and is a no-op.
            self.assertEqual(revalidate_lessons(
                semantic, project_id=PROJECT, revision="d" * 40,
                evidence_rows=self.valid_rows(),
                graph_updated_at="2026-07-18T06:00:00Z",
            ), [])
            self.assertEqual(len((semantic / "lessons.jsonl").read_text().splitlines()), 2)

    def test_revalidation_index_rejects_symlink_parent_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"; real.mkdir()
            alias = root / "alias"; alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(EvidenceValidationError):
                RevalidationIndex(alias / "revalidation.sqlite3")
            index = RevalidationIndex(root / "private/revalidation.sqlite3")
            self.assertEqual(index.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(index.path.parent.stat().st_mode & 0o777, 0o700)

    def test_revalidation_index_rebuilds_links_after_db_recreation(self):
        candidate = translate_memos_record("skill", owned(
            id="skill-rebuild", name="Rebuild reverse links",
            invocationGuide="Recover stale mappings from lifecycle artifacts.",
            evidenceAnchors=["evi_" + "1" * 64],
        ), PROJECT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); graduated = root / "candidates/graduated"
            graduated.mkdir(parents=True)
            (graduated / f"{candidate['id']}.json").write_text(json.dumps(candidate))
            index = RevalidationIndex(root / "evidence/revalidation.sqlite3")
            self.assertEqual(index.rebuild_from_candidates(root / "candidates"), 1)
            self.assertEqual(index.mark_evidence_stale(
                ["evi_" + "1" * 64], "graph drift", "event-rebuild",
            ), [candidate["id"]])
            index.clear_provider_stale("memos-local", ["skill-rebuild"])
            self.assertEqual(index.mark_evidence_stale(
                ["evi_" + "1" * 64], "second same-revision drift", "event-rebuild",
            ), [candidate["id"]])
            self.assertTrue(index.is_provider_stale("memos-local", "skill-rebuild"))

    def test_retrieval_outcome_maps_only_memos_ids(self):
        candidate = translate_memos_record("skill", owned(
            id="skill-outcome", name="Outcome-linked guidance",
            invocationGuide="Update counts only from MemOS retrieval identities.",
        ), PROJECT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidates = root / "memory/candidates"
            candidates.mkdir(parents=True)
            path = candidates / f"{candidate['id']}.json"
            path.write_text(json.dumps(candidate))
            unrelated = SimpleNamespace(
                event_type="retrieval.used", event_id="outcome-0",
                payload={"outcome": "used", "item_ids": [
                    "governance:lesson-1", "evidence:evi_123",
                ]},
            )
            self.assertEqual(record_retrieval_outcome(root, unrelated), [])
            linked = SimpleNamespace(
                event_type="retrieval.used", event_id="outcome-1",
                payload={"outcome": "contradicted", "item_ids": [
                    "governance:lesson-1", "memos:skill-outcome",
                ]},
            )
            self.assertEqual(record_retrieval_outcome(root, linked), [candidate["id"]])
            saved = json.loads(path.read_text())
            self.assertEqual(saved["contradiction_count"], 1)
            self.assertEqual(saved["status"], "staged")
            invalid = SimpleNamespace(
                event_type="retrieval.used", event_id="outcome-invalid",
                payload={"outcome": "surprising", "item_ids": ["memos:skill-outcome"]},
            )
            self.assertEqual(record_retrieval_outcome(root, invalid), [])

    def test_outcome_update_never_resurrects_revalidation_state(self):
        candidate = translate_memos_record("skill", owned(
            id="skill-no-resurrect", name="Never resurrect stale guidance",
            invocationGuide="Outcome accounting must preserve latest trust state.",
        ), PROJECT)
        candidate["status"] = "accepted"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidates = root / "memory/candidates/graduated"
            semantic = root / "memory/semantic"
            candidates.mkdir(parents=True); semantic.mkdir(parents=True)
            (candidates / f"{candidate['id']}.json").write_text(json.dumps(candidate))
            lesson = {
                "id": "lesson_stale", "claim": candidate["claim"],
                "source_candidate": candidate["id"],
                "status": "revalidation_needed",
            }
            (semantic / "lessons.jsonl").write_text(json.dumps(lesson) + "\n")
            event = SimpleNamespace(
                event_type="retrieval.used", event_id="outcome-stale",
                payload={"outcome": "used", "item_ids": ["memos:skill-no-resurrect"]},
            )
            self.assertEqual(record_retrieval_outcome(root, event), [candidate["id"]])
            rows = [json.loads(line) for line in
                    (semantic / "lessons.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[-1]["status"], "revalidation_needed")

    def test_live_validator_can_tombstone_same_revision_drift(self):
        lesson = {
            "id": "lesson_live", "claim": "Use a current linked symbol.",
            "status": "accepted", "accepted_at": "2026-07-18T06:00:00Z",
            "evidence_ids": ["evi_" + "1" * 64, "evi_" + "2" * 64],
            "project_scope": {"project_id": PROJECT}, "code_specific": True,
            "code_refs": [{"file_path": "pkg.py", "qualified_name": "pkg.fn"}],
            "evidence_snapshot": {"repository_revision": REVISION},
        }
        with tempfile.TemporaryDirectory() as tmp:
            semantic = Path(tmp)
            (semantic / "lessons.jsonl").write_text(json.dumps(lesson) + "\n")
            (semantic / "LESSONS.md").write_text(
                "## Auto-promoted entries will be appended below\n"
            )
            def drift(_candidate):
                raise EvidenceValidationError("linked CRG symbols are stale")
            self.assertEqual(revalidate_lessons(
                semantic, project_id=PROJECT, revision=REVISION,
                evidence_rows=self.valid_rows(),
                graph_updated_at="2026-07-18T06:00:00Z",
                live_validator=drift,
            ), ["lesson_live"])

    def test_live_validation_rejects_symbol_hash_drift_and_dirty_link(self):
        candidate = {
            **self.candidate(),
            "code_refs": [{"file_path": "pkg.py", "qualified_name": "pkg.fn"}],
        }
        health = {
            "status": "healthy", "repository_revision": REVISION,
            "graph_updated_at": "2026-07-18T06:00:00Z", "database": "graph.db",
        }

        class Provider:
            def __init__(self, **_kwargs): pass
            def health(self): return health
            def _validate_symbols(self, _symbols, _database): return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "pkg.py").write_text("def fn(): pass\n")
            ledger = root / "ledger.jsonl"
            ledger.write_text("".join(json.dumps(row) + "\n" for row in self.valid_rows()))
            with mock.patch("orchestration.revalidation.derive_project_identity",
                            return_value=SimpleNamespace(project_id=PROJECT)), \
                 mock.patch("orchestration.revalidation.CrgEvidenceProvider", Provider), \
                 mock.patch("orchestration.revalidation.subprocess.run",
                            return_value=SimpleNamespace(returncode=0, stdout=" M pkg.py\n")):
                with self.assertRaisesRegex(EvidenceValidationError, "uncommitted"):
                    validate_live_candidate_evidence(
                        candidate, root, repo_root=root, ledger_path=ledger,
                    )

            class DriftProvider(Provider):
                def _validate_symbols(self, _symbols, _database):
                    from orchestration.providers.crg_evidence import CrgEvidenceError
                    raise CrgEvidenceError("file hash drift")

            with mock.patch("orchestration.revalidation.derive_project_identity",
                            return_value=SimpleNamespace(project_id=PROJECT)), \
                 mock.patch("orchestration.revalidation.CrgEvidenceProvider", DriftProvider):
                with self.assertRaisesRegex(EvidenceValidationError, "symbols are stale"):
                    validate_live_candidate_evidence(
                        candidate, root, repo_root=root, ledger_path=ledger,
                    )

    def test_renderer_shows_only_latest_append_only_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            semantic = Path(tmp)
            rows = [
                {"id": "lesson_one", "claim": "Use bounded retries.",
                 "status": "provisional", "accepted_at": "2026-07-18T00:00:00Z"},
                {"id": "lesson_one", "claim": "Use bounded retries.",
                 "status": "accepted", "accepted_at": "2026-07-18T01:00:00Z"},
                {"id": "lesson_one", "claim": "Use bounded retries.",
                 "status": "revalidation_needed", "accepted_at": "2026-07-18T01:00:00Z"},
            ]
            (semantic / "lessons.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            rendered = Path(render_lessons(semantic)).read_text()
            self.assertEqual(rendered.count("Use bounded retries."), 1)
            self.assertIn("[REVALIDATION NEEDED]", rendered)


class ReviewerWorkflowTest(unittest.TestCase):
    def test_inspect_reconstructs_provider_and_evidence_chain(self):
        review = load_tool("memory_review")
        candidate = translate_memos_record("skill", owned(
            id="skill-inspect", name="Inspect evidence guidance",
            invocationGuide="Show provider identity and resolved evidence together.",
            evidenceAnchors=["evi_" + "1" * 64],
        ), PROJECT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidates = root / "candidates"
            semantic = root / "semantic"; evidence_dir = root / "evidence"
            candidates.mkdir(); semantic.mkdir(); evidence_dir.mkdir()
            (candidates / f"{candidate['id']}.json").write_text(json.dumps(candidate))
            ledger = evidence_dir / "ledger.jsonl"
            ledger.write_text(json.dumps(evidence("evi_" + "1" * 64, "crg_node")) + "\n")
            with mock.patch.object(review, "BASE", root), \
                 mock.patch.object(review, "CANDIDATES", candidates), \
                 mock.patch.object(review, "SEMANTIC", semantic), \
                 mock.patch.object(review, "LEDGER", ledger):
                report = review.inspect_candidate(candidate["id"])
            self.assertEqual(report["provider_ids"], {"skill_id": "skill-inspect"})
            self.assertEqual(report["evidence_chain"][0]["status"], "resolved")
            self.assertEqual(report["evidence_chain"][0]["record"]["evidence_id"],
                             "evi_" + "1" * 64)

    def test_graduate_blocks_code_candidate_before_any_semantic_write(self):
        graduate = load_tool("graduate")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates"
            semantic = root / "semantic"
            candidates.mkdir(); semantic.mkdir()
            candidate = translate_memos_record("skill", owned(
                id="skill-code", name="Verified code repair",
                invocationGuide="Use only with current code evidence.",
                codeSpecific=True, codeRefs=[
                    {"file_path": "pkg.py", "qualified_name": "pkg.fn"},
                ], evidenceAnchors=["evi_" + "1" * 64], support=2, gain=.1,
            ), PROJECT)
            candidate["decisions"].append({
                "ts": "2026-07-18T00:00:00Z", "action": "classified_code",
                "reviewer": "human", "notes": "explicit code classification",
            })
            (candidates / f"{candidate['id']}.json").write_text(json.dumps(candidate))
            (semantic / "LESSONS.md").write_text("## Auto-promoted entries will be appended below\n")
            with mock.patch.object(graduate, "CANDIDATES", str(candidates)), \
                 mock.patch.object(graduate, "SEMANTIC", str(semantic)), \
                 mock.patch.object(graduate, "validate_live_candidate_evidence",
                                   side_effect=EvidenceValidationError("stale")), \
                 mock.patch.object(sys, "argv", [
                     "graduate.py", candidate["id"], "--rationale", "human review",
                 ]), self.assertRaises(SystemExit) as stopped:
                graduate._main_unlocked()
            self.assertEqual(stopped.exception.code, 4)
            self.assertFalse((semantic / "lessons.jsonl").exists())
            self.assertTrue((candidates / f"{candidate['id']}.json").exists())

    def test_behavioral_graduation_requires_explicit_classification(self):
        graduate = load_tool("graduate")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidates = root / "candidates"; semantic = root / "semantic"
            candidates.mkdir(); semantic.mkdir()
            candidate = translate_memos_record("policy", owned(
                id="policy-classify", title="Classify before acceptance",
                procedure="A human must confirm whether guidance is code-specific.",
            ), PROJECT)
            (candidates / f"{candidate['id']}.json").write_text(json.dumps(candidate))
            with mock.patch.object(graduate, "CANDIDATES", str(candidates)), \
                 mock.patch.object(graduate, "SEMANTIC", str(semantic)), \
                 mock.patch.object(sys, "argv", [
                     "graduate.py", candidate["id"], "--rationale", "reviewed",
                 ]), self.assertRaises(SystemExit) as stopped:
                graduate._main_unlocked()
            self.assertEqual(stopped.exception.code, 5)
            self.assertFalse((semantic / "lessons.jsonl").exists())

    def test_review_classification_is_explicit_and_immutable_after_graduation(self):
        review = load_tool("memory_review")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidates = root / "candidates"
            graduated = candidates / "graduated"; graduated.mkdir(parents=True)
            candidate = translate_memos_record("policy", owned(
                id="policy-scope", title="Classify code scope explicitly",
                procedure="Human review binds file and qualified symbol references.",
            ), PROJECT)
            active = candidates / f"{candidate['id']}.json"
            active.write_text(json.dumps(candidate))
            with mock.patch.object(review, "CANDIDATES", candidates):
                review.classify_candidate(
                    candidate["id"], "senior", ["pkg.py::pkg.fn"], False,
                )
                saved = json.loads(active.read_text())
                self.assertTrue(saved["code_specific"])
                self.assertEqual(saved["code_refs"][0]["qualified_name"], "pkg.fn")
                active.replace(graduated / active.name)
                with self.assertRaisesRegex(ValueError, "cannot be changed"):
                    review.classify_candidate(
                        candidate["id"], "senior", [], True,
                    )

    def test_reviewer_can_replace_revision_bound_evidence_before_reacceptance(self):
        review = load_tool("memory_review")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidates = root / "candidates"
            graduated = candidates / "graduated"; graduated.mkdir(parents=True)
            candidate = translate_memos_record("skill", owned(
                id="skill-refresh", name="Refresh revision evidence",
                invocationGuide="Attach current graph and executed-test evidence.",
                evidenceAnchors=["evi_" + "1" * 64],
            ), PROJECT)
            candidate.update(
                status="revalidation_needed", code_specific=True,
                code_refs=[{"file_path": "pkg.py", "qualified_name": "pkg.fn"}],
            )
            path = graduated / f"{candidate['id']}.json"
            path.write_text(json.dumps(candidate))
            new_refs = ["evi_" + "3" * 64, "evi_" + "4" * 64]
            with mock.patch.object(review, "CANDIDATES", candidates):
                review.refresh_evidence(candidate["id"], "senior", new_refs)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["evidence_refs"], new_refs)
            rows = [
                evidence(new_refs[0], "crg_node"),
                evidence(new_refs[1], "test_run", executed=True),
            ]
            self.assertTrue(validate_candidate_evidence(
                saved, rows, project_id=PROJECT, revision=REVISION,
                graph_updated_at="2026-07-18T06:00:00Z",
            )["eligible"])

    def test_revalidation_needed_candidate_can_be_explicitly_reaccepted(self):
        review = load_tool("memory_review")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); candidates = root / "candidates"
            graduated = candidates / "graduated"; semantic = root / "semantic"
            graduated.mkdir(parents=True); semantic.mkdir()
            candidate = translate_memos_record("skill", owned(
                id="skill-reaccept", name="Reaccept current guidance",
                invocationGuide="Accept again only after live evidence validation.",
                evidenceAnchors=["evi_" + "1" * 64],
            ), PROJECT)
            candidate.update(status="revalidation_needed", code_specific=True,
                             code_refs=[{"file_path": "pkg.py", "qualified_name": "pkg.fn"}])
            candidate["decisions"].append({
                "ts": "2026-07-18T00:00:00Z", "action": "classified_code",
                "reviewer": "human",
            })
            (graduated / f"{candidate['id']}.json").write_text(json.dumps(candidate))
            lesson = {
                "id": "lesson_reaccept", "claim": candidate["claim"],
                "source_candidate": candidate["id"], "status": "revalidation_needed",
                "evidence_snapshot": {"repository_revision": "old"},
            }
            (semantic / "lessons.jsonl").write_text(json.dumps(lesson) + "\n")
            (semantic / "LESSONS.md").write_text(
                "## Auto-promoted entries will be appended below\n"
            )
            index = RevalidationIndex(root / "memory/evidence/revalidation.sqlite3")
            index.link_candidate(candidate)
            index.mark_evidence_stale(
                ["evi_" + "1" * 64], "old evidence", "event-old",
            )
            self.assertTrue(index.is_provider_stale("memos-local", "skill-reaccept"))
            new_refs = ["evi_" + "3" * 64, "evi_" + "4" * 64]
            with mock.patch.object(review, "BASE", root), \
                 mock.patch.object(review, "CANDIDATES", candidates), \
                 mock.patch.object(review, "SEMANTIC", semantic), \
                 mock.patch.object(review, "validate_live_candidate_evidence",
                                   return_value={"repository_revision": REVISION}):
                review.refresh_evidence(candidate["id"], "senior", new_refs)
                accepted = review.finalize_graduated(
                    candidate["id"], "evidence rechecked", "senior",
                )
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["evidence_ids"], new_refs)
            saved = json.loads((graduated / f"{candidate['id']}.json").read_text())
            self.assertEqual(saved["status"], "accepted")
            self.assertFalse(index.is_provider_stale("memos-local", "skill-reaccept"))
            current_rows = [
                evidence(new_refs[0], "crg_node"),
                evidence(new_refs[1], "test_run", executed=True),
            ]
            self.assertEqual(revalidate_lessons(
                semantic, project_id=PROJECT, revision=REVISION,
                evidence_rows=current_rows,
                graph_updated_at="2026-07-18T06:00:00Z",
            ), [])

    def test_provisional_can_be_explicitly_finalized(self):
        review = load_tool("memory_review")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates"; graduated = candidates / "graduated"
            semantic = root / "semantic"
            graduated.mkdir(parents=True); semantic.mkdir()
            candidate = translate_memos_record("policy", owned(
                id="policy-provisional", title="Use bounded retries",
                procedure="Retry only after checking the failure class.",
                support=3, gain=.2,
            ), PROJECT)
            candidate["status"] = "provisional"
            (graduated / f"{candidate['id']}.json").write_text(json.dumps(candidate))
            lesson = {
                "id": "lesson_" + candidate["id"], "claim": candidate["claim"],
                "status": "provisional", "source_candidate": candidate["id"],
                "reviewer": "host-agent", "rationale": "trial",
            }
            (semantic / "lessons.jsonl").write_text(json.dumps(lesson) + "\n")
            (semantic / "LESSONS.md").write_text("## Auto-promoted entries will be appended below\n")
            with mock.patch.object(review, "CANDIDATES", candidates), \
                 mock.patch.object(review, "SEMANTIC", semantic):
                accepted = review.finalize_provisional(
                    candidate["id"], "evidence reviewed", "senior",
                )
            self.assertEqual(accepted["status"], "accepted")
            saved = json.loads((graduated / f"{candidate['id']}.json").read_text())
            self.assertEqual(saved["status"], "accepted")
            rows = [json.loads(line) for line in (semantic / "lessons.jsonl").read_text().splitlines()]
            self.assertEqual(rows[-1]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
