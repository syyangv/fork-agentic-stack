from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from harness_manager import transfer_tui


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "docs" / "backup-restore-uninstall-retention.md"


class Phase9OperationsDocumentationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = OPERATIONS.read_text(encoding="utf-8")
        self.normalized = " ".join(self.text.replace("**", "").split())

    def test_operations_guide_is_discoverable_from_readme(self) -> None:
        self.assertTrue(OPERATIONS.is_file())
        self.assertIn(
            "docs/backup-restore-uninstall-retention.md",
            (ROOT / "README.md").read_text(encoding="utf-8"),
        )

    def test_backup_restore_contract_matches_the_public_python_api(self) -> None:
        for required in (
            "memos_backup.create_project_backup",
            "memos_backup.restore_project_backup",
            "There is no backup or restore CLI subcommand.",
            "lifecycle lock",
            "close the bridge",
            "manifest.json",
            "SHA-256",
            "atomic swap",
            "rollback tree",
            "owner-only",
        ):
            self.assertIn(required, self.normalized)
        source = (ROOT / ".agent/memory/orchestration/memos_backup.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("def create_project_backup(", source)
        self.assertIn("def restore_project_backup(", source)
        self.assertIn("BACKUP_SCHEMA = \"agentic.memory.memos-backup.v1\"", source)

    def test_rollback_tree_permission_claim_matches_atomic_rename_behavior(self) -> None:
        self.assertIn(
            "returned rollback tree preserves the former target's existing modes",
            self.normalized,
        )
        self.assertIn("Keep its parent owner-controlled", self.normalized)
        self.assertIn("inspect and tighten permissions if needed", self.normalized)
        self.assertNotIn("owner-only rollback tree", self.normalized)
        source = (ROOT / ".agent/memory/orchestration/memos_backup.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("os.replace(target, rollback)", source)
        self.assertNotIn("os.chmod(rollback", source)

    def test_transfer_crg_and_behavioral_boundaries_are_explicit(self) -> None:
        for required in (
            "execute_import_transaction",
            "preferences, decisions, accepted lessons, and the validated bounded evidence ledger",
            "CRG graph databases, caches, registries, snapshots, indexes, and derived state are never transferred",
            "Rebuild CRG graph locally after import; no CRG graph database or cache was transferred.",
            "export_behavioral_artifact",
            "--behavioral-export",
            "non-authoritative",
            "no import or activation route",
        ):
            self.assertIn(required, self.normalized)
        tui_source = (ROOT / "harness_manager/transfer_tui.py").read_text(encoding="utf-8")
        bundle_source = (ROOT / "harness_manager/transfer_bundle.py").read_text(encoding="utf-8")
        self.assertIn("def execute_import_transaction", tui_source)
        self.assertIn("Rebuild CRG graph locally after import; no CRG graph database or cache was transferred.", bundle_source)

    def test_profile_remove_and_phase8_safety_claims_are_bounded(self) -> None:
        for required in (
            "standard",
            "minimal",
            "phase8_quality_gate: blocked",
            "MemOS capability may be present but remains off",
            "governance-only",
            "evolution is disabled",
            "R7 skill is not promoted",
            "agentic-stack remove <adapter>",
            "preserves .agent governance data and profile state",
            "agentic-stack doctor",
        ):
            self.assertIn(required, self.normalized)
        profile_source = (ROOT / "harness_manager/profiles.py").read_text(encoding="utf-8")
        self.assertIn('PHASE8_QUALITY_GATE = "blocked"', profile_source)
        self.assertIn('"memos_mode": "off"', profile_source)

    def test_retention_limit_is_honest_about_the_unenforced_policy_gap(self) -> None:
        for required in (
            "retentionDays: 30",
            "profiles/<project_id>/memos-plugin/config.yaml",
            "not an enforced system-wide retention policy",
            "180-day error/audit-summary retention target is not enforced",
            "manual policy gap",
            "has not verified or enforced automatic 30-day cleanup",
            "no automated 30/180-day retention job",
        ):
            self.assertIn(required, self.normalized)
        self.assertNotIn("what the managed provider honors", self.normalized)
        runtime_source = (ROOT / ".agent/memory/orchestration/memos_runtime.py").read_text(
            encoding="utf-8",
        )
        self.assertIn('"retentionDays": 30', runtime_source)

    def test_r7_evidence_is_immutable_non_authoritative_and_never_reused(self) -> None:
        for required in (
            "/Users/syang/.agent/runtime/memos/5efa1310d8759984/evaluation/r7",
            "/Users/syang/.agent/runtime/backups/phase8-r7-complete-20260728T234651Z",
            "No R7 task, verifier, or run identity may be reused as future held-out evidence",
            "fresh never-exported corpus and new approval",
            "R7 remains non-authoritative and activation remains blocked",
        ):
            self.assertIn(required, self.normalized)

    def test_documented_cli_surfaces_exist_in_current_help(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            transfer_tui.print_help()
        help_text = output.getvalue()
        self.assertIn("--behavioral-export", help_text)
        self.assertIn("rebuild CRG locally after import", help_text)
        for required in ("agentic-stack transfer", "agentic-stack doctor", "agentic-stack remove <adapter>"):
            self.assertIn(required, self.normalized)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
