import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".agent" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from knowledge_core.capture import CANDIDATE_BLOCK_END, CANDIDATE_BLOCK_START, capture_result
from knowledge_core.config import FORMAL_DIR_ALLOWLIST, VaultRegistration
from knowledge_core.drafts import DraftStore


WORK_SOURCE = {
    "source_id": "source_synthetic_p5_001",
    "vault_id": "work",
    "note_id": "note_synthetic_p5_001",
    "path": "10-Projects/synthetic.md",
    "heading": "Boundary",
    "line_start": 1,
    "line_end": 8,
    "content_hash": "a" * 64,
}


def candidate_payload(**overrides):
    payload = {
        "summary": "The synthetic task established a bounded knowledge boundary.",
        "candidate_notes": [
            "Keep source provenance separate from generated draft content.",
        ],
        "source_refs": [WORK_SOURCE],
        "validation_evidence": ["Agent reported the result after the completed task."],
        "uncertainties": ["External validation is still pending."],
    }
    payload.update(overrides)
    return payload


def candidate_output(payload=None, suffix=""):
    body = json.dumps(payload or candidate_payload(), ensure_ascii=False)
    return f"ordinary answer that must not be persisted\n{CANDIDATE_BLOCK_START}\n{body}\n{CANDIDATE_BLOCK_END}\n{suffix}"


class TestKnowledgeCapture(unittest.TestCase):
    def test_duplicate_completion_is_idempotent_and_does_not_store_full_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "runtime" / "drafts.sqlite3")
            output = candidate_output(suffix="secret outside candidate block")

            first = capture_result(
                "task_synthetic_p5_001",
                "turn_synthetic_p5_001",
                output,
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )
            second = capture_result(
                "task_synthetic_p5_001",
                "turn_synthetic_p5_001",
                output,
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )

            self.assertEqual(first.capture_status, "draft_created")
            self.assertEqual(second.capture_status, "duplicate")
            self.assertEqual(first.draft["draft_id"], second.draft["draft_id"])
            self.assertEqual(len(store.list_drafts()), 1)
            self.assertNotIn("secret outside candidate block", json.dumps(store.list_drafts()))

    def test_missing_invalid_and_oversized_candidates_are_capture_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "drafts.sqlite3")

            missing = capture_result(
                "task_missing",
                "turn_missing",
                '{"summary":"not a delimited candidate"}',
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )
            invalid = capture_result(
                "task_invalid",
                "turn_invalid",
                candidate_output({"summary": "missing required fields"}),
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )
            oversized = capture_result(
                "task_oversized",
                "turn_oversized",
                candidate_output(candidate_payload(summary="x" * 10_001)),
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )

            self.assertEqual(missing.capture_status, "capture_incomplete")
            self.assertIn("candidate_block_missing", missing.diagnostic_codes)
            self.assertEqual(invalid.capture_status, "capture_incomplete")
            self.assertIn("candidate_schema_invalid", invalid.diagnostic_codes)
            self.assertEqual(oversized.capture_status, "capture_incomplete")
            self.assertIn("field_oversize", oversized.diagnostic_codes)
            self.assertEqual(store.list_drafts(), [])

    def test_personal_context_is_quarantined_without_persisting_candidate_content(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "drafts.sqlite3")
            personal_source = {**WORK_SOURCE, "vault_id": "personal", "path": "Journal/private.md"}
            private_text = "private synthetic text must never reach the draft store"
            output = candidate_output(
                candidate_payload(candidate_notes=[private_text], source_refs=[WORK_SOURCE, personal_source])
            )

            result = capture_result(
                "task_personal",
                "turn_personal",
                output,
                task_status="completed",
                turn_status="completed",
                personal_context_used=True,
                source_manifest=[WORK_SOURCE, personal_source],
                draft_store=store,
            )

            self.assertEqual(result.capture_status, "quarantined")
            self.assertIsNone(result.draft)
            self.assertEqual(store.list_drafts(), [])
            serialized_records = json.dumps(
                {"captures": store.list_captures(), "drafts": store.list_drafts()},
                ensure_ascii=False,
            )
            self.assertNotIn("Journal/private.md", serialized_records)
            self.assertNotIn(private_text, serialized_records)
            captures = store.list_captures()
            self.assertEqual(len(captures), 1)
            self.assertTrue(captures[0]["personal_context_used"])
            self.assertEqual(captures[0]["provenance"], [WORK_SOURCE])

    def test_personal_candidate_requires_a_separate_approved_persistence_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "drafts.sqlite3")
            personal_source = {**WORK_SOURCE, "vault_id": "personal", "path": "Journal/private.md"}
            decision = {
                "status": "approved",
                "approved_by": "human-synthetic-reviewer",
                "approved_at": "2026-09-13T15:00:00Z",
                "scope": "candidate_content",
            }

            result = capture_result(
                "task_personal_approved",
                "turn_personal_approved",
                candidate_output(candidate_payload(source_refs=[personal_source])),
                task_status="completed",
                turn_status="completed",
                personal_context_used=True,
                source_manifest=[personal_source],
                persistence_decision=decision,
                draft_store=store,
            )

            self.assertEqual(result.capture_status, "draft_created")
            self.assertEqual(len(store.list_drafts()), 1)
            self.assertEqual(store.list_drafts()[0]["source_refs"], [personal_source])
            self.assertEqual(store.list_captures()[0]["provenance"], [])

    def test_failed_or_cancelled_turn_never_becomes_a_successful_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "drafts.sqlite3")
            result = capture_result(
                "task_failed",
                "turn_failed",
                candidate_output(),
                task_status="failed",
                turn_status="canceled",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )

            self.assertEqual(result.capture_status, "capture_incomplete")
            self.assertIn("turn_not_completed", result.diagnostic_codes)
            self.assertIsNone(result.draft)
            self.assertEqual(store.list_drafts(), [])

    def test_missing_turn_status_fails_closed_without_writing_a_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "drafts.sqlite3")
            result = capture_result(
                "task_missing_turn_status",
                "turn_missing_status",
                candidate_output(),
                task_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )

            self.assertEqual(result.capture_status, "capture_incomplete")
            self.assertIn("turn_not_completed", result.diagnostic_codes)
            self.assertIsNone(result.draft)
            self.assertEqual(store.list_drafts(), [])

    def test_incomplete_capture_metadata_redacts_personal_paths_and_excerpts(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "drafts.sqlite3")
            personal_source = {**WORK_SOURCE, "vault_id": "personal", "path": "Journal/private.md"}
            private_excerpt = "synthetic private journal excerpt must not be persisted"

            cases = (
                {
                    "task_id": "task_missing_turn_status_privacy",
                    "turn_id": "turn_missing_turn_status_privacy",
                    "result_text": candidate_output(
                        candidate_payload(candidate_notes=[private_excerpt], source_refs=[personal_source])
                    ),
                    "task_status": "completed",
                    "source_manifest": [WORK_SOURCE, personal_source],
                    # Deliberately omit turn_status: this is the reproduced P7 blocker.
                },
                {
                    "task_id": "task_invalid_result_privacy",
                    "turn_id": "turn_invalid_result_privacy",
                    "result_text": "not a delimited candidate",
                    "task_status": "completed",
                    "turn_status": "completed",
                    "source_manifest": [WORK_SOURCE, personal_source],
                },
                {
                    "task_id": "task_cancelled_privacy",
                    "turn_id": "turn_cancelled_privacy",
                    "result_text": candidate_output(
                        candidate_payload(candidate_notes=[private_excerpt], source_refs=[personal_source])
                    ),
                    "task_status": "failed",
                    "turn_status": "cancelled",
                    "source_manifest": [personal_source],
                },
            )

            for case in cases:
                result = capture_result(
                    case.pop("task_id"),
                    case.pop("turn_id"),
                    case.pop("result_text"),
                    draft_store=store,
                    **case,
                )
                self.assertEqual(result.capture_status, "capture_incomplete")

            serialized_captures = json.dumps(store.list_captures(), ensure_ascii=False)
            self.assertNotIn("Journal/private.md", serialized_captures)
            self.assertNotIn(private_excerpt, serialized_captures)
            self.assertEqual(
                [capture["provenance"] for capture in store.list_captures()],
                [[WORK_SOURCE], [WORK_SOURCE], []],
            )

    def test_source_provenance_and_target_action_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "drafts.sqlite3")
            unknown_source = {**WORK_SOURCE, "source_id": "source_not_in_manifest"}
            unknown = capture_result(
                "task_source",
                "turn_source",
                candidate_output(candidate_payload(source_refs=[unknown_source])),
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )
            formal_target = capture_result(
                "task_target",
                "turn_target",
                candidate_output(candidate_payload(target_path="10-Projects/not-allowed.md")),
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
            )

            self.assertEqual(unknown.capture_status, "capture_incomplete")
            self.assertIn("source_ref_unrecognized", unknown.diagnostic_codes)
            self.assertEqual(formal_target.capture_status, "capture_incomplete")
            self.assertIn("target_invalid", formal_target.diagnostic_codes)
            self.assertEqual(store.list_drafts(), [])

    def test_work_draft_is_written_only_to_inbox_and_never_formal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            for directory in ("00-Inbox", *FORMAL_DIR_ALLOWLIST):
                (vault / directory).mkdir(parents=True, exist_ok=True)
            registration = VaultRegistration(
                vault_id="work",
                path=vault,
                is_personal=False,
                indexed=True,
                allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
            )
            store = DraftStore(root / "runtime" / "drafts.sqlite3")

            result = capture_result(
                "task_work",
                "turn_work",
                candidate_output(),
                task_status="completed",
                turn_status="completed",
                source_manifest=[WORK_SOURCE],
                draft_store=store,
                draft_vault=registration,
            )

            self.assertEqual(result.capture_status, "draft_created")
            self.assertTrue(result.draft["target_path"].startswith("00-Inbox/"))
            draft_path = vault / result.draft["target_path"]
            self.assertEqual(draft_path.read_text(encoding="utf-8"), result.draft["content"])
            self.assertFalse(list((vault / "10-Projects").iterdir()))
            self.assertNotIn(WORK_SOURCE["path"], result.draft["content"])
            self.assertEqual(result.draft["validation_level"], "agent_reported")

    def test_personal_candidate_never_writes_an_inbox_draft_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "work-vault"
            (vault / "00-Inbox").mkdir(parents=True)
            for directory in FORMAL_DIR_ALLOWLIST:
                (vault / directory).mkdir()
            registration = VaultRegistration(
                vault_id="work",
                path=vault,
                is_personal=False,
                indexed=True,
                allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
            )
            store = DraftStore(root / "drafts.sqlite3")
            personal_source = {**WORK_SOURCE, "vault_id": "personal", "path": "Journal/private.md"}
            private_text = "private synthetic text must never reach the work vault"
            result = capture_result(
                "task_personal_file",
                "turn_personal_file",
                candidate_output(candidate_payload(candidate_notes=[private_text], source_refs=[personal_source])),
                task_status="completed",
                turn_status="completed",
                personal_context_used=True,
                source_manifest=[personal_source],
                draft_store=store,
                draft_vault=registration,
            )

            self.assertEqual(result.capture_status, "quarantined")
            self.assertEqual(list((vault / "00-Inbox").iterdir()), [])
            self.assertEqual([path for path in vault.rglob("*") if path.is_file()], [])

    def test_propose_note_cli_captures_json_envelope_without_a_vault_path(self):
        with tempfile.TemporaryDirectory() as temp:
            store_path = Path(temp) / "runtime" / "drafts.sqlite3"
            envelope = {
                "result": candidate_output(),
                "source_manifest": [WORK_SOURCE],
                "personal_context_used": False,
                "turn_status": "completed",
            }
            command = [
                sys.executable,
                str(ROOT / ".agent" / "tools" / "knowledge.py"),
                "propose_note",
                "--task-id",
                "task_cli",
                "--turn-id",
                "turn_cli",
                "--task-status",
                "completed",
                "--draft-store",
                str(store_path),
            ]
            first = subprocess.run(command, input=json.dumps(envelope), capture_output=True, text=True, check=False)
            second = subprocess.run(command, input=json.dumps(envelope), capture_output=True, text=True, check=False)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(first.stdout)["capture_status"], "draft_created")
            self.assertEqual(json.loads(second.stdout)["capture_status"], "duplicate")
            with DraftStore(store_path) as store:
                self.assertEqual(len(store.list_drafts()), 1)


if __name__ == "__main__":
    unittest.main()
