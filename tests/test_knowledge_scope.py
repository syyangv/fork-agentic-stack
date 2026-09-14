import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".agent" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


class TestKnowledgeScope(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        self.work_vault_dir = self.test_dir / "work-vault"
        self.personal_vault_dir = self.test_dir / "personal-vault"
        self.work_vault_dir.mkdir(parents=True)
        self.personal_vault_dir.mkdir(parents=True)

        for d in ["00-Inbox", "10-Projects", "20-Research", "30-Decisions", "40-Lessons", "90-Archive"]:
            (self.work_vault_dir / d).mkdir()

        # Populate a sample personal vault
        (self.personal_vault_dir / "Journal").mkdir()
        (self.personal_vault_dir / "Journal" / "2026-09-13.md").write_text("Private notes", encoding="utf-8")
        (self.personal_vault_dir / "SharedProject").mkdir()
        (self.personal_vault_dir / "SharedProject" / "spec.md").write_text("Spec content", encoding="utf-8")
        (self.personal_vault_dir / "Secrets").mkdir()
        (self.personal_vault_dir / "Secrets" / "keys.md").write_text("Secret keys", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_formal_dir_allowlist_and_scope(self):
        from knowledge_core.config import FORMAL_DIR_ALLOWLIST, VaultRegistration
        from knowledge_core.scope import validate_vault_path

        work_vault = VaultRegistration(
            vault_id="work",
            path=self.work_vault_dir,
            is_personal=False,
            indexed=True,
            allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
        )

        # Valid formal directories
        for d in FORMAL_DIR_ALLOWLIST:
            ok, msg, path = validate_vault_path(work_vault, f"{d}/note.md", require_formal=True)
            self.assertTrue(ok, f"Expected {d}/note.md to be allowed: {msg}")

        # Invalid / non-formal directories
        for invalid in ["00-Inbox/draft.md", "90-Archive/old.md", ".obsidian/app.json", "random/file.md"]:
            ok, msg, path = validate_vault_path(work_vault, invalid, require_formal=True)
            self.assertFalse(ok, f"Expected {invalid} to be rejected from formal scope")
            self.assertIn("formal", msg.lower())

    def test_canonical_path_checks_rejection_of_traversal_and_symlinks(self):
        from knowledge_core.config import FORMAL_DIR_ALLOWLIST, VaultRegistration
        from knowledge_core.scope import validate_vault_path

        work_vault = VaultRegistration(
            vault_id="work",
            path=self.work_vault_dir,
            is_personal=False,
            indexed=True,
            allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
        )

        # 1. Traversal attempts via ..
        traversals = [
            "../outside.md",
            "10-Projects/../../outside.md",
            "10-Projects/../00-Inbox/secret.md",
            "20-Research/../../../etc/passwd",
        ]
        for t in traversals:
            ok, msg, path = validate_vault_path(work_vault, t, require_formal=False)
            self.assertFalse(ok, f"Expected traversal {t} to be rejected: {msg}")

        # 2. Absolute path injection
        abs_paths = [
            "/etc/passwd",
            str(self.personal_vault_dir / "Journal" / "2026-09-13.md"),
        ]
        for ap in abs_paths:
            ok, msg, path = validate_vault_path(work_vault, ap, require_formal=False)
            self.assertFalse(ok, f"Expected absolute path {ap} to be rejected: {msg}")

        # 3. Symlink pointing outside vault
        outside_target = self.test_dir / "external_secret.md"
        outside_target.write_text("Top secret", encoding="utf-8")
        symlink_file = self.work_vault_dir / "10-Projects" / "symlink_note.md"
        symlink_file.symlink_to(outside_target)

        ok, msg, path = validate_vault_path(work_vault, "10-Projects/symlink_note.md", require_formal=False)
        self.assertFalse(ok, "Expected symlink pointing outside vault to be rejected")
        self.assertIn("symlink", msg.lower())

        # 4. Directory symlink pointing outside vault (parent symlink check)
        outside_dir = self.test_dir / "external_folder"
        outside_dir.mkdir()
        (outside_dir / "leaked.md").write_text("leaked", encoding="utf-8")
        symlink_dir = self.work_vault_dir / "10-Projects" / "external_link_dir"
        symlink_dir.symlink_to(outside_dir)

        ok, msg, path = validate_vault_path(work_vault, "10-Projects/external_link_dir/leaked.md", require_formal=False)
        self.assertFalse(ok, "Expected parent symlink pointing outside vault to be rejected")
        self.assertIn("symlink", msg.lower())

    def test_internal_symlink_canonical_formal_allowlist_regression(self):
        from knowledge_core.config import FORMAL_DIR_ALLOWLIST, VaultRegistration
        from knowledge_core.scope import validate_vault_path

        work_vault = VaultRegistration(
            vault_id="work",
            path=self.work_vault_dir,
            is_personal=False,
            indexed=True,
            allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
        )

        # 1. File symlink: 10-Projects/sneaky_draft.md -> ../00-Inbox/draft.md (existing target)
        draft_note = self.work_vault_dir / "00-Inbox" / "draft.md"
        draft_note.write_text("Unapproved draft", encoding="utf-8")
        sneaky_file_link = self.work_vault_dir / "10-Projects" / "sneaky_draft.md"
        sneaky_file_link.symlink_to(draft_note)

        ok, msg, canon_path = validate_vault_path(work_vault, "10-Projects/sneaky_draft.md", require_formal=True)
        self.assertFalse(ok, "Internal symlink pointing to non-formal directory (00-Inbox) must be rejected")
        self.assertIn("formal directory allowlist", msg)

        # 2. Dangling file symlink: 10-Projects/dangling_draft_link.md -> ../00-Inbox/future_draft.md (target DOES NOT exist)
        # Canonical check must deny this even before the target exists!
        dangling_draft_link = self.work_vault_dir / "10-Projects" / "dangling_draft_link.md"
        dangling_draft_link.symlink_to(self.work_vault_dir / "00-Inbox" / "future_draft.md")

        ok, msg, canon_path = validate_vault_path(work_vault, "10-Projects/dangling_draft_link.md", require_formal=True)
        self.assertFalse(ok, "Dangling symlink pointing to non-formal directory must be denied before target exists")
        self.assertIn("formal directory allowlist", msg)

        # 3. Dangling symlink pointing outside vault entirely (target does not exist)
        dangling_outside_link = self.work_vault_dir / "10-Projects" / "dangling_outside_link.md"
        dangling_outside_link.symlink_to(self.test_dir / "non_existent_outside_file.md")

        ok, msg, canon_path = validate_vault_path(work_vault, "10-Projects/dangling_outside_link.md", require_formal=False)
        self.assertFalse(ok, "Dangling symlink pointing outside vault root must be rejected")
        self.assertIn("outside vault", msg.lower())

        # 4. Directory symlink: 10-Projects/inbox_alias -> ../00-Inbox
        inbox_dir_link = self.work_vault_dir / "10-Projects" / "inbox_alias"
        inbox_dir_link.symlink_to(self.work_vault_dir / "00-Inbox")

        ok, msg, canon_path = validate_vault_path(work_vault, "10-Projects/inbox_alias/draft.md", require_formal=True)
        self.assertFalse(ok, "Directory symlink resolving to non-formal dir must be rejected from formal scope")
        self.assertIn("formal directory allowlist", msg)

        # 5. Legitimate internal symlink within formal directory:
        # 10-Projects/alias_to_project.md -> 10-Projects/valid_project.md
        valid_project = self.work_vault_dir / "10-Projects" / "valid_project.md"
        valid_project.write_text("Approved project knowledge", encoding="utf-8")
        valid_link = self.work_vault_dir / "10-Projects" / "alias_to_project.md"
        valid_link.symlink_to(valid_project)

        ok, msg, canon_path = validate_vault_path(work_vault, "10-Projects/alias_to_project.md", require_formal=True)
        self.assertTrue(ok, f"Legitimate internal symlink within formal directory should be allowed: {msg}")
        self.assertEqual(canon_path, valid_project.resolve())

    def test_stable_note_id_generation_and_identification(self):
        from knowledge_core.id_manager import (
            extract_note_id,
            identify_note,
            ensure_note_id,
            generate_note_id,
        )

        # 1. ID generation format
        id1 = generate_note_id()
        id2 = generate_note_id()
        self.assertTrue(id1.startswith("k-"))
        self.assertNotEqual(id1, id2)

        # 2. Extract ID from frontmatter
        content_with_id = "---\nid: k-20260913-abc123\ntitle: Test Note\n---\nBody here\n"
        self.assertEqual(extract_note_id(content_with_id), "k-20260913-abc123")

        content_without_id = "---\ntitle: Manual Note\n---\nBody here\n"
        self.assertIsNone(extract_note_id(content_without_id))

        content_no_frontmatter = "# Just Markdown\nSome text"
        self.assertIsNone(extract_note_id(content_no_frontmatter))

        # 3. Identify note: uses ID when present, falls back to path identifier without modifying note
        rel_path = Path("10-Projects/Architecture.md")
        self.assertEqual(identify_note(rel_path, content_with_id), "k-20260913-abc123")
        self.assertEqual(identify_note(rel_path, content_without_id), "10-Projects/Architecture.md")

        # 4. ensure_note_id: only adds ID when creating or generating a note
        new_content, assigned_id = ensure_note_id(content_without_id)
        self.assertTrue(assigned_id.startswith("k-"))
        self.assertEqual(extract_note_id(new_content), assigned_id)
        self.assertIn("title: Manual Note", new_content)

        # Calling ensure_note_id on note already having ID preserves existing ID
        same_content, existing_id = ensure_note_id(content_with_id)
        self.assertEqual(existing_id, "k-20260913-abc123")
        self.assertEqual(same_content, content_with_id)

    def test_task_scoped_personal_grants_access_controls_and_traversal(self):
        from knowledge_core.config import VaultRegistration
        from knowledge_core.grants import TaskGrantManager

        personal_vault = VaultRegistration(
            vault_id="personal",
            path=self.personal_vault_dir,
            is_personal=True,
            indexed=False,
            allowed_dirs=[],
        )

        grant_mgr = TaskGrantManager()
        self.assertFalse(grant_mgr.is_persisted())

        # Without grant, all personal vault access is denied
        self.assertFalse(grant_mgr.check_access("task-101", "SharedProject/spec.md", personal_vault))
        self.assertFalse(grant_mgr.check_access("task-101", "Journal/2026-09-13.md", personal_vault))

        # Grant specific note to task-101
        grant_mgr.grant("task-101", "SharedProject/spec.md", personal_vault)

        # 1. task-101 can access granted note
        self.assertTrue(grant_mgr.check_access("task-101", "SharedProject/spec.md", personal_vault))

        # 2. task-101 CANNOT access ungranted sibling notes in same vault
        self.assertFalse(grant_mgr.check_access("task-101", "Secrets/keys.md", personal_vault))
        self.assertFalse(grant_mgr.check_access("task-101", "Journal/2026-09-13.md", personal_vault))

        # 3. Another task (task-102) CANNOT access task-101's granted note (task isolation)
        self.assertFalse(grant_mgr.check_access("task-102", "SharedProject/spec.md", personal_vault))

        # 4. Directory grant: grant entire SharedProject/ directory to task-202
        grant_mgr.grant("task-202", "SharedProject", personal_vault)
        self.assertTrue(grant_mgr.check_access("task-202", "SharedProject/spec.md", personal_vault))
        self.assertFalse(grant_mgr.check_access("task-202", "Secrets/keys.md", personal_vault))

        # 5. Attempt to grant path outside personal vault must fail
        with self.assertRaises(ValueError):
            grant_mgr.grant("task-303", "../work-vault/10-Projects", personal_vault)

        # 6. Revocation
        grant_mgr.revoke("task-101")
        self.assertFalse(grant_mgr.check_access("task-101", "SharedProject/spec.md", personal_vault))

    def test_task_scoped_personal_grants_cross_vault_denial_and_mocked_expiry(self):
        from knowledge_core.config import VaultRegistration
        from knowledge_core.grants import TaskGrantManager

        # Setup two distinct personal vaults with identical relative path structure
        vault_a_dir = self.test_dir / "personal_vault_a"
        vault_b_dir = self.test_dir / "personal_vault_b"
        vault_a_dir.mkdir()
        vault_b_dir.mkdir()

        (vault_a_dir / "SharedProject").mkdir()
        (vault_a_dir / "SharedProject" / "spec.md").write_text("Spec Vault A", encoding="utf-8")
        (vault_a_dir / "Journal").mkdir()
        (vault_a_dir / "Journal" / "2026-09-13.md").write_text("Journal Vault A", encoding="utf-8")

        (vault_b_dir / "SharedProject").mkdir()
        (vault_b_dir / "SharedProject" / "spec.md").write_text("Spec Vault B", encoding="utf-8")

        vault_a = VaultRegistration(vault_id="personal_a", path=vault_a_dir, is_personal=True, indexed=False)
        vault_b = VaultRegistration(vault_id="personal_b", path=vault_b_dir, is_personal=True, indexed=False)

        # Controllable deterministic clock (not relying on real-time sleep)
        clock_state = {"now": 1000.0}
        mgr1 = TaskGrantManager(clock=lambda: clock_state["now"])

        # 1. TTL validation: zero, negative, nan, and inf must all fail closed (raise ValueError)
        for invalid_ttl in [0, -1, -0.5, float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(ValueError) as ctx:
                mgr1.grant("task-ttl", "SharedProject/spec.md", vault_a, expires_in_seconds=invalid_ttl)
            self.assertIn("positive finite number", str(ctx.exception).lower())

        # 2. Grant spec.md in Vault A to task-101
        mgr1.grant("task-101", "SharedProject/spec.md", vault_a)

        # Allowed in Vault A
        self.assertTrue(mgr1.check_access("task-101", "SharedProject/spec.md", vault_a))

        # Cross-vault denial: Same relative path in Vault B MUST be denied
        self.assertFalse(
            mgr1.check_access("task-101", "SharedProject/spec.md", vault_b),
            "Grant in Vault A must never authorize access in Vault B, even with identical relative paths",
        )

        # 3. Conflicting canonical root under same vault_id must be rejected
        vault_a_clashing = VaultRegistration(vault_id="personal_a", path=vault_b_dir, is_personal=True, indexed=False)
        with self.assertRaises(ValueError) as ctx:
            mgr1.grant("task-101", "SharedProject/spec.md", vault_a_clashing)
        self.assertIn("conflicting vault roots", str(ctx.exception).lower())
        self.assertFalse(mgr1.check_access("task-101", "SharedProject/spec.md", vault_a_clashing))

        # 4. Expiry test with mocked clock
        mgr1.grant("task-expiring", "SharedProject/spec.md", vault_a, expires_in_seconds=60.0)
        clock_state["now"] = 1030.0  # +30s: still active
        self.assertTrue(mgr1.check_access("task-expiring", "SharedProject/spec.md", vault_a))

        clock_state["now"] = 1061.0  # +61s: expired!
        self.assertFalse(mgr1.check_access("task-expiring", "SharedProject/spec.md", vault_a))

        # 5. Revocation: specific vault vs all vaults
        mgr1.grant("task-multi", "SharedProject/spec.md", vault_a)
        mgr1.grant("task-multi", "SharedProject/spec.md", vault_b)
        self.assertTrue(mgr1.check_access("task-multi", "SharedProject/spec.md", vault_a))
        self.assertTrue(mgr1.check_access("task-multi", "SharedProject/spec.md", vault_b))

        # Revoke only vault_a
        mgr1.revoke("task-multi", vault_id="personal_a")
        self.assertFalse(mgr1.check_access("task-multi", "SharedProject/spec.md", vault_a))
        self.assertTrue(mgr1.check_access("task-multi", "SharedProject/spec.md", vault_b))

        # Revoke all for task
        mgr1.revoke("task-multi")
        self.assertFalse(mgr1.check_access("task-multi", "SharedProject/spec.md", vault_b))

        # 6. Separate manager isolation
        mgr2 = TaskGrantManager(clock=lambda: clock_state["now"])
        self.assertFalse(mgr2.check_access("task-101", "SharedProject/spec.md", vault_a))

    def test_task_scoped_personal_grants_expired_regrant_without_intervening_check_access(self):
        """
        Verify that calling grant() on an expired task directly purges the expired paths,
        even without any intervening check_access() call during the expiration window.
        """
        from knowledge_core.config import VaultRegistration
        from knowledge_core.grants import TaskGrantManager

        vault_dir = self.test_dir / "personal_vault_regrant"
        vault_dir.mkdir()
        (vault_dir / "DocA.md").write_text("Doc A", encoding="utf-8")
        (vault_dir / "DocB.md").write_text("Doc B", encoding="utf-8")

        vault = VaultRegistration(vault_id="personal_rg", path=vault_dir, is_personal=True, indexed=False)

        clock_state = {"now": 2000.0}
        mgr = TaskGrantManager(clock=lambda: clock_state["now"])

        # 1. Grant DocA with TTL of 60 seconds
        grant1 = mgr.grant("task-no-check", "DocA.md", vault, expires_in_seconds=60.0)
        self.assertEqual(grant1.granted_paths, {"DocA.md"})

        # 2. Advance clock past expiration (+80s) WITHOUT calling check_access
        clock_state["now"] = 2080.0

        # 3. Directly call grant for DocB without any intervening check_access call
        grant2 = mgr.grant("task-no-check", "DocB.md", vault, expires_in_seconds=60.0)

        # 4. Prove grant itself purged the expired path:
        # The grant's paths must strictly contain ONLY DocB, never reviving DocA
        self.assertEqual(grant2.granted_paths, {"DocB.md"})
        self.assertTrue(mgr.check_access("task-no-check", "DocB.md", vault))
        self.assertFalse(
            mgr.check_access("task-no-check", "DocA.md", vault),
            "Granting to an expired task without intervening check_access must never resurrect expired paths",
        )


if __name__ == "__main__":
    unittest.main()
