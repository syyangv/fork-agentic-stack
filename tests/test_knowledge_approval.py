import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".agent" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from knowledge_core.approval import (  # noqa: E402
    ApplyConflictError,
    ApprovalApplier,
    ApprovalRequiredError,
    TargetSafetyError,
    TerminalDraftError,
)
from knowledge_core.capture import CANDIDATE_BLOCK_END, CANDIDATE_BLOCK_START, capture_result  # noqa: E402
from knowledge_core.config import FORMAL_DIR_ALLOWLIST, VaultRegistration  # noqa: E402
from knowledge_core.drafts import DraftStore  # noqa: E402
from knowledge_core.index import KnowledgeIndex  # noqa: E402
from knowledge_core.retrieval import KnowledgeRetriever  # noqa: E402


SOURCE = {
    "source_id": "source_p6_synthetic",
    "vault_id": "work",
    "note_id": "note_p6_synthetic",
    "path": "10-Projects/source.md",
    "heading": "P6",
    "line_start": 1,
    "line_end": 3,
    "content_hash": "a" * 64,
}


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TestKnowledgeApproval(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.vault = root / "work-vault"
        for directory in ("00-Inbox", *FORMAL_DIR_ALLOWLIST, "90-Archive"):
            (self.vault / directory).mkdir(parents=True)
        self.store = DraftStore(root / "runtime" / "drafts.sqlite3")
        self.journal = root / "runtime" / "apply.jsonl"
        self.registration = VaultRegistration(
            vault_id="work",
            path=self.vault,
            is_personal=False,
            indexed=True,
            allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
        )

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def add_draft(self, *, draft_id="draft-p6", target_path="00-Inbox/p6.md", action="create", base=None, content="# Draft\n"):
        draft = {
            "draft_id": draft_id,
            "task_id": "task-p6",
            "turn_id": "turn-p6",
            "candidate_hash": sha256(draft_id),
            "target_path": target_path,
            "target_action": action,
            "title": "P6 synthetic",
            "content": content,
            "source_refs": [SOURCE],
            "validation_evidence": [{"kind": "agent_reported", "evidence": "synthetic"}],
            "validation_level": "agent_reported",
            "uncertainties": ["synthetic"],
        }
        if base is not None:
            draft["target_base_hash"] = base
        stored, created = self.store.put_draft(draft)
        self.assertTrue(created)
        return stored

    def applier(self, **kwargs):
        return ApprovalApplier(self.registration, self.store, self.journal, **kwargs)

    def human(self, decision="accept", *, draft_hash=None, target_hash=None, target_path=None):
        value = {"decision": decision, "human": True, "approved_by": "synthetic-reviewer"}
        if draft_hash is not None:
            value["confirmed_draft_hash"] = draft_hash
        if target_hash is not None:
            value["confirmed_target_hash"] = target_hash
        if target_path is not None:
            value["target_path"] = target_path
        return value

    def test_apply_requires_explicit_formal_target_choice_for_new_note(self):
        draft = self.add_draft()
        applier = self.applier()
        with self.assertRaises(ApprovalRequiredError):
            applier.apply(draft["draft_id"], draft_hash=draft["draft_hash"], target_hash=None)
        with self.assertRaises(TargetSafetyError):
            applier.apply(
                draft["draft_id"],
                draft_hash=draft["draft_hash"],
                target_hash=None,
                human_approval=self.human(draft_hash=draft["draft_hash"]),
            )
        with self.assertRaises(TargetSafetyError):
            applier.apply(
                draft["draft_id"],
                draft_hash=draft["draft_hash"],
                target_hash=None,
                target_path="00-Inbox/automatic.md",
                human_approval=self.human(draft_hash=draft["draft_hash"], target_path="00-Inbox/automatic.md"),
            )
        with self.assertRaises(ApprovalRequiredError):
            applier.apply(
                draft["draft_id"],
                draft_hash=draft["draft_hash"],
                target_hash=None,
                target_path="10-Projects/other.md",
                human_approval=self.human(
                    draft_hash=draft["draft_hash"],
                    target_path="10-Projects/not-the-selected-path.md",
                ),
            )
        target_path = "10-Projects/accepted.md"
        target = self.vault / target_path
        self.assertEqual(self.store.get_draft(draft["draft_id"])["status"], "draft")

        result = applier.apply(
            draft["draft_id"],
            draft_hash=draft["draft_hash"],
            target_hash=None,
            target_path=target_path,
            human_approval=self.human(draft_hash=draft["draft_hash"], target_path=target_path),
        )
        self.assertEqual(result.status, "accepted")
        self.assertFalse(result.idempotent)
        self.assertEqual(target.read_text(encoding="utf-8"), draft["content"])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        accepted = self.store.get_draft(draft["draft_id"])
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["source_refs"], [SOURCE])
        reviews = self.store.list_reviews(draft_id=draft["draft_id"])
        self.assertEqual(reviews[0]["decision"], "accept")
        self.assertNotIn("session", json.dumps(reviews))

        repeated = applier.apply(
            draft["draft_id"],
            draft_hash=draft["draft_hash"],
            target_hash=None,
            target_path=target_path,
            human_approval=self.human(draft_hash=draft["draft_hash"], target_path=target_path),
        )
        self.assertTrue(repeated.idempotent)
        self.assertEqual(target.read_text(encoding="utf-8").count("# Draft"), 1)

    def test_edit_then_accept_binds_new_content_hash(self):
        draft = self.add_draft(target_path="10-Projects/edited.md")
        edited = "# Human-edited\n\nApproved wording.\n"
        edited_hash = sha256(edited)
        result = self.applier().apply(
            draft["draft_id"],
            draft_hash=edited_hash,
            target_hash=None,
            target_path="10-Projects/edited.md",
            content=edited,
            human_approval=self.human(draft_hash=edited_hash, target_path="10-Projects/edited.md"),
        )
        self.assertEqual(result.desired_hash, edited_hash)
        self.assertEqual((self.vault / "10-Projects/edited.md").read_text(encoding="utf-8"), edited)
        self.assertEqual(self.store.get_draft(draft["draft_id"])["content"], draft["content"])
        self.assertEqual(self.store.list_reviews(draft_id=draft["draft_id"])[0]["desired_hash"], edited_hash)

    def test_reject_is_terminal_and_does_not_write(self):
        draft = self.add_draft()
        rejected = self.applier().reject(
            draft["draft_id"],
            reason="Needs stronger evidence.",
            human_approval=self.human("reject"),
        )
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(TerminalDraftError):
            self.applier().apply(
                draft["draft_id"], draft_hash=draft["draft_hash"], target_hash=None, human_approval=True
            )
        self.assertFalse((self.vault / draft["target_path"]).exists())
        self.assertEqual(self.store.list_reviews(draft_id=draft["draft_id"])[0]["reason"], "Needs stronger evidence.")

    def test_new_target_collision_stops_without_overwrite(self):
        draft = self.add_draft()
        target_path = "10-Projects/collision.md"
        target = self.vault / target_path
        target.write_text("external\n", encoding="utf-8")
        with self.assertRaises(ApplyConflictError):
            self.applier().apply(
                draft["draft_id"],
                draft_hash=draft["draft_hash"],
                target_hash=None,
                target_path=target_path,
                human_approval=self.human(target_path=target_path),
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "external\n")
        self.assertEqual(self.store.get_draft(draft["draft_id"])["status"], "draft")

    def test_update_requires_stored_base_hash_and_rechecks_stale_target(self):
        original = "# Existing\n"
        target = self.vault / "10-Projects/existing.md"
        target.write_text(original, encoding="utf-8")
        base = sha256(original)
        draft = self.add_draft(target_path="10-Projects/existing.md", action="update", base=base)
        target.write_text("# External edit\n", encoding="utf-8")
        with self.assertRaises(ApplyConflictError):
            self.applier().apply(
                draft["draft_id"],
                draft_hash=draft["draft_hash"],
                target_hash=base,
                human_approval=self.human(target_hash=base),
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "# External edit\n")
        self.assertEqual(self.store.get_draft(draft["draft_id"])["status"], "draft")

    def test_update_success_and_index_refresh_is_path_scoped(self):
        original = "# Existing\n"
        target = self.vault / "10-Projects/existing.md"
        target.write_text(original, encoding="utf-8")
        base = sha256(original)
        draft = self.add_draft(target_path="10-Projects/existing.md", action="single_file_update", base=base)
        refreshed = []
        result = self.applier(index_refresh=refreshed.append).apply(
            draft["draft_id"],
            draft_hash=draft["draft_hash"],
            target_hash=base,
            human_approval=self.human(target_hash=base),
        )
        self.assertEqual(result.status, "accepted")
        self.assertEqual(refreshed, ["10-Projects/existing.md"])
        self.assertEqual(target.read_text(encoding="utf-8"), draft["content"])

    def test_traversal_archive_config_attachment_and_symlink_are_rejected(self):
        for index, path in enumerate(("../outside.md", "/tmp/absolute.md", "90-Archive/a.md", "config/a.md", "attachments/a.md")):
            draft = self.add_draft(draft_id=f"unsafe-{index}", target_path=path)
            with self.assertRaises(TargetSafetyError):
                self.applier().apply(
                    draft["draft_id"], draft_hash=draft["draft_hash"], target_hash=None, human_approval=True
                )
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        link = self.vault / "00-Inbox/link.md"
        link.symlink_to(outside / "note.md")
        draft = self.add_draft(draft_id="unsafe-link", target_path="00-Inbox/link.md")
        with self.assertRaises(TargetSafetyError):
            self.applier().apply(draft["draft_id"], draft_hash=draft["draft_hash"], target_hash=None, human_approval=True)
        self.assertFalse((outside / "note.md").exists())

    def test_interrupted_write_recovers_once_without_duplicate_content(self):
        draft = self.add_draft()
        target_path = "20-Research/recovered.md"

        def interrupt(event):
            if event == "after_target_write":
                raise RuntimeError("synthetic interruption")

        with self.assertRaises(RuntimeError):
            self.applier(event_hook=interrupt).apply(
                draft["draft_id"],
                draft_hash=draft["draft_hash"],
                target_hash=None,
                target_path=target_path,
                human_approval=self.human(target_path=target_path),
            )
        target = self.vault / target_path
        self.assertEqual(target.read_text(encoding="utf-8"), draft["content"])
        self.assertEqual(self.store.get_draft(draft["draft_id"])["status"], "draft")
        recovered = self.applier().recover()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(self.store.get_draft(draft["draft_id"])["status"], "accepted")
        self.assertEqual(self.applier().recover(), [])
        self.assertEqual(target.read_text(encoding="utf-8").count("# Draft"), 1)

    def test_recovery_conflict_requires_manual_resolution_and_never_overwrites(self):
        draft = self.add_draft()
        target_path = "20-Research/recovery-conflict.md"

        def interrupt(event):
            if event == "after_target_write":
                raise RuntimeError("synthetic interruption")

        with self.assertRaises(RuntimeError):
            self.applier(event_hook=interrupt).apply(
                draft["draft_id"],
                draft_hash=draft["draft_hash"],
                target_hash=None,
                target_path=target_path,
                human_approval=self.human(target_path=target_path),
            )
        target = self.vault / target_path
        target.write_text("manual resolution\n", encoding="utf-8")
        with self.assertRaises(ApplyConflictError):
            self.applier().recover()
        self.assertEqual(target.read_text(encoding="utf-8"), "manual resolution\n")
        self.assertEqual(self.store.get_draft(draft["draft_id"])["status"], "draft")

    def test_t18_capture_approve_formal_note_refreshes_and_retrieves_later(self):
        payload = {
            "summary": "T18 accepted knowledge marker",
            "candidate_notes": ["T18 approved marker is retrievable from formal knowledge."],
            "source_refs": [SOURCE],
            "validation_evidence": ["synthetic completed task"],
            "uncertainties": [],
        }
        output = (
            "ordinary answer\n"
            + CANDIDATE_BLOCK_START
            + "\n"
            + json.dumps(payload)
            + "\n"
            + CANDIDATE_BLOCK_END
        )
        captured = capture_result(
            "task_t18",
            "turn_t18",
            output,
            task_status="completed",
            turn_status="completed",
            source_manifest=[SOURCE],
            draft_store=self.store,
            draft_vault=self.registration,
        )
        self.assertEqual(captured.capture_status, "draft_created")
        draft = captured.draft
        self.assertIsNotNone(draft)
        self.assertTrue(draft["target_path"].startswith("00-Inbox/"))

        index = KnowledgeIndex(self.journal.parent / "index.sqlite3")
        index.sync(self.registration)
        target_path = "20-Research/t18-accepted.md"
        result = self.applier(index=index).apply(
            draft["draft_id"],
            draft_hash=draft["draft_hash"],
            target_hash=None,
            target_path=target_path,
            human_approval=self.human(draft_hash=draft["draft_hash"], target_path=target_path),
        )
        self.assertTrue(result.index_updated)
        self.assertEqual(result.target_path, target_path)
        self.assertEqual(self.store.get_draft(draft["draft_id"])["status"], "accepted")
        self.assertEqual(
            [item["path"] for item in KnowledgeRetriever(index, work_vault=self.registration).search("T18 approved marker").results],
            [target_path],
        )
        self.assertEqual(
            KnowledgeRetriever(index, work_vault=self.registration).search("T18 accepted knowledge marker").results[0]["path"],
            target_path,
        )


if __name__ == "__main__":
    unittest.main()
