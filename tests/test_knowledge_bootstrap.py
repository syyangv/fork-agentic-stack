import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".agent" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

KNOWLEDGE_CLI = TOOLS / "knowledge.py"


class TestKnowledgeBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        self.work_vault = self.test_dir / "work-vault"
        self.personal_vault = self.test_dir / "personal-vault"
        self.runtime_dir = self.test_dir / "runtime"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bootstrap_initializes_expected_directories_and_files(self):
        from knowledge_core.bootstrap import init_work_vault
        from knowledge_core.config import FORMAL_DIR_ALLOWLIST

        result = init_work_vault(self.work_vault)
        self.assertTrue(result["success"])
        self.assertTrue(self.work_vault.is_dir())

        # Check standard directories
        expected_dirs = ["00-Inbox", "90-Archive"] + list(FORMAL_DIR_ALLOWLIST)
        for dir_name in expected_dirs:
            target_dir = self.work_vault / dir_name
            self.assertTrue(target_dir.is_dir(), f"Expected directory {dir_name} to exist")

        # Check Home.md and 00-Inbox/README.md
        home_file = self.work_vault / "Home.md"
        self.assertTrue(home_file.is_file(), "Expected Home.md to exist")
        home_content = home_file.read_text(encoding="utf-8")
        self.assertIn("Agent Knowledge Vault", home_content)
        # Check rule reference policy (规则只链接、不镜像)
        self.assertIn("只链接", home_content)

        inbox_readme = self.work_vault / "00-Inbox" / "README.md"
        self.assertTrue(inbox_readme.is_file(), "Expected 00-Inbox/README.md to exist")
        inbox_content = inbox_readme.read_text(encoding="utf-8")
        self.assertIn("草稿", inbox_content)

        # Check template directory and files
        templates_dir = self.work_vault / "_templates"
        self.assertTrue(templates_dir.is_dir(), "Expected _templates directory to exist")
        for template_name in ["project.md", "research.md", "decision.md", "lesson.md"]:
            template_path = templates_dir / template_name
            self.assertTrue(template_path.is_file(), f"Expected template {template_name} to exist")
            tmpl_content = template_path.read_text(encoding="utf-8")
            self.assertIn("---", tmpl_content)
            self.assertIn("id:", tmpl_content)

    def test_bootstrap_refuses_nonempty_destination(self):
        from knowledge_core.bootstrap import init_work_vault

        # Setup nonempty destination with existing user file
        self.work_vault.mkdir(parents=True, exist_ok=True)
        user_file = self.work_vault / "important_user_doc.txt"
        user_file.write_text("Crucial user content that must never be overwritten", encoding="utf-8")

        # 1. Direct function call must raise FileExistsError by default
        with self.assertRaises(FileExistsError) as ctx:
            init_work_vault(self.work_vault)
        self.assertIn("not empty", str(ctx.exception).lower())
        self.assertIn("refused", str(ctx.exception).lower())

        # Verify preexisting file remains untouched and no vault dirs were created
        self.assertEqual(user_file.read_text(encoding="utf-8"), "Crucial user content that must never be overwritten")
        self.assertFalse((self.work_vault / "Home.md").exists())
        self.assertFalse((self.work_vault / "00-Inbox").exists())

        # 2. CLI call must exit non-zero and report refusal in JSON (using sys.executable)
        res = subprocess.run(
            [sys.executable, str(KNOWLEDGE_CLI), "init", "--work-vault", str(self.work_vault), "--runtime-dir", str(self.runtime_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res.returncode, 0)
        cli_err = json.loads(res.stdout)
        self.assertFalse(cli_err["success"])
        self.assertTrue(cli_err["refused"])
        self.assertIn("not empty", cli_err["error"].lower())

    def test_bootstrap_rejects_escaping_symlink_destinations_and_preserves_outside_dir(self):
        from knowledge_core.bootstrap import init_work_vault

        self.work_vault.mkdir(parents=True, exist_ok=True)

        # 1. Test 00-Inbox symlinked to existing outside directory
        outside_inbox = self.test_dir / "external_inbox_dir"
        outside_inbox.mkdir()
        canary_file = outside_inbox / "canary.txt"
        canary_file.write_text("outside canary content", encoding="utf-8")

        symlink_inbox = self.work_vault / "00-Inbox"
        symlink_inbox.symlink_to(outside_inbox)

        # Preflight must reject this escaping symlink even with allow_nonempty=True
        with self.assertRaises(ValueError) as ctx:
            init_work_vault(self.work_vault, allow_nonempty=True)
        self.assertIn("escaping symlink", str(ctx.exception).lower())

        # Crucial regression invariant: outside directory must remain completely unchanged
        self.assertEqual(canary_file.read_text(encoding="utf-8"), "outside canary content")
        self.assertFalse((outside_inbox / "README.md").exists(), "README.md must not be written into outside directory")

        # 2. Test _templates symlinked to existing outside directory
        symlink_inbox.unlink()
        outside_templates = self.test_dir / "external_templates_dir"
        outside_templates.mkdir()
        canary_tmpl = outside_templates / "custom.txt"
        canary_tmpl.write_text("custom", encoding="utf-8")

        symlink_templates = self.work_vault / "_templates"
        symlink_templates.symlink_to(outside_templates)

        with self.assertRaises(ValueError) as ctx:
            init_work_vault(self.work_vault, allow_nonempty=True)
        self.assertIn("escaping symlink", str(ctx.exception).lower())
        self.assertFalse((outside_templates / "project.md").exists(), "project.md must not be written outside vault")

    def test_bootstrap_preflight_rejects_planned_dir_file_collisions_before_any_writes(self):
        """
        Verify that if a planned directory collides with an existing regular file,
        preflight rejects before any files (e.g. Home.md) or directories (00-Inbox) are created.
        """
        from knowledge_core.bootstrap import init_work_vault

        self.work_vault.mkdir(parents=True, exist_ok=True)

        # Pre-create planned directory '10-Projects' as a regular file (collision)
        colliding_file = self.work_vault / "10-Projects"
        colliding_file.write_text("canary file content", encoding="utf-8")

        # Preflight must reject this before writing any files or directories
        with self.assertRaises(FileExistsError) as ctx:
            init_work_vault(self.work_vault, allow_nonempty=True)
        self.assertIn("collides with an existing non-directory file", str(ctx.exception))

        # Assert vault remains unmodified: Home.md was NOT written, 00-Inbox was NOT created, canary intact
        self.assertEqual(colliding_file.read_text(encoding="utf-8"), "canary file content")
        self.assertFalse((self.work_vault / "Home.md").exists(), "Home.md must not be created on preflight failure")
        self.assertFalse((self.work_vault / "00-Inbox").exists(), "00-Inbox must not be created on preflight failure")

    def test_bootstrap_preflight_rejects_invalid_runtime_destinations_before_vault_writes(self):
        from knowledge_core.bootstrap import init_work_vault

        # Setup an invalid runtime destination whose parent is a regular file (cannot mkdir underneath it)
        parent_file = self.test_dir / "invalid_parent_file.txt"
        parent_file.write_text("not a directory", encoding="utf-8")
        bad_runtime = parent_file / "runtime_sub"

        # Preflight must reject this before writing to the vault
        with self.assertRaises(NotADirectoryError) as ctx:
            init_work_vault(self.work_vault, runtime_dir=bad_runtime)
        self.assertIn("runtime ancestor", str(ctx.exception).lower())

        # Assert work vault was never created
        self.assertFalse(self.work_vault.exists(), "Work vault must not be created when runtime preflight fails")

    def test_bootstrap_rejects_unsafe_and_dangling_symlink_destinations(self):
        from knowledge_core.bootstrap import init_work_vault

        # 1. Target vault is a dangling symlink
        dangling_vault = self.test_dir / "dangling_vault_link"
        dangling_vault.symlink_to(self.test_dir / "non_existent_target")
        with self.assertRaises(ValueError) as ctx:
            init_work_vault(dangling_vault)
        self.assertIn("dangling symlink", str(ctx.exception).lower())

        # 2. Ancestor of target vault is a dangling symlink
        dangling_parent = self.test_dir / "dangling_parent_link"
        dangling_parent.symlink_to(self.test_dir / "non_existent_parent")
        child_of_dangling = dangling_parent / "vault"
        with self.assertRaises(ValueError) as ctx:
            init_work_vault(child_of_dangling)
        self.assertIn("dangling symlink", str(ctx.exception).lower())

    def test_bootstrap_never_touches_real_vaults(self):
        from knowledge_core.bootstrap import init_work_vault

        # Use sentinel paths to prove default paths are not created when explicit paths are supplied.
        # (Note: absent sentinel proves no creation, not no read).
        with tempfile.TemporaryDirectory() as guard_dir:
            sentinel_work = Path(guard_dir) / "guarded_work_vault"
            sentinel_runtime = Path(guard_dir) / "guarded_runtime_dir"

            with mock.patch("knowledge_core.config.DEFAULT_WORK_VAULT_PATH", sentinel_work), \
                 mock.patch("knowledge_core.config.DEFAULT_RUNTIME_DIR", sentinel_runtime):
                res = init_work_vault(self.work_vault, runtime_dir=self.runtime_dir)
                self.assertTrue(res["success"])

                # Verify sentinel default paths were never created
                self.assertFalse(sentinel_work.exists(), "Default work vault path was created when explicit path was given")
                self.assertFalse(sentinel_runtime.exists(), "Default runtime dir was created when explicit path was given")

    def test_bootstrap_is_strictly_non_overwriting_when_allowed(self):
        from knowledge_core.bootstrap import init_work_vault

        # Pre-create Home.md with custom content
        self.work_vault.mkdir(parents=True, exist_ok=True)
        custom_home = self.work_vault / "Home.md"
        custom_home.write_text("My Custom Home Note - Do Not Overwrite", encoding="utf-8")

        # Pre-create a note in a formal dir
        projects_dir = self.work_vault / "10-Projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        custom_project = projects_dir / "ExistingProject.md"
        custom_project.write_text("Existing Project Content", encoding="utf-8")

        # Run init with allow_nonempty=True (uses exclusive mode="x" non-overwriting creation)
        result = init_work_vault(self.work_vault, allow_nonempty=True)
        self.assertTrue(result["success"])
        self.assertIn("Home.md", result["skipped"])

        # Verify custom files were preserved untouched
        self.assertEqual(custom_home.read_text(encoding="utf-8"), "My Custom Home Note - Do Not Overwrite")
        self.assertEqual(custom_project.read_text(encoding="utf-8"), "Existing Project Content")

    def test_preflight_rejects_runtime_ancestor_symlink_to_file_before_vault_writes(self):
        from knowledge_core.bootstrap import init_work_vault

        regular = self.test_dir / "runtime-target-file"
        regular.write_text("not a directory", encoding="utf-8")
        runtime_link = self.test_dir / "runtime-link"
        runtime_link.symlink_to(regular)
        runtime_path = runtime_link / "nested-runtime"

        with self.assertRaises((NotADirectoryError, ValueError)):
            init_work_vault(self.work_vault, runtime_dir=runtime_path)

        # Runtime preflight must happen before any vault files or directories are created.
        self.assertFalse(self.work_vault.exists())

    def test_cli_status_and_init(self):
        # 1. Check status on uninitialized vault (using sys.executable)
        res_status_uninit = subprocess.run(
            [sys.executable, str(KNOWLEDGE_CLI), "status", "--work-vault", str(self.work_vault), "--runtime-dir", str(self.runtime_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_status_uninit.returncode, 0, f"status stderr: {res_status_uninit.stderr}")
        data = json.loads(res_status_uninit.stdout)
        self.assertEqual(data["status"], "uninitialized")
        self.assertFalse(data["work_vault"]["exists"])
        self.assertFalse(data["personal_vault"]["indexed"])
        self.assertTrue(data["personal_vault"]["grant_required"])

        # 2. Run init via CLI on new/empty path
        res_init = subprocess.run(
            [sys.executable, str(KNOWLEDGE_CLI), "init", "--work-vault", str(self.work_vault), "--runtime-dir", str(self.runtime_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_init.returncode, 0, f"init stderr: {res_init.stderr}")
        init_data = json.loads(res_init.stdout)
        self.assertTrue(init_data["success"])
        self.assertTrue(len(init_data["created"]) > 0)

        # 3. Check status on initialized vault
        res_status_init = subprocess.run(
            [sys.executable, str(KNOWLEDGE_CLI), "status", "--work-vault", str(self.work_vault), "--runtime-dir", str(self.runtime_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_status_init.returncode, 0)
        data_after = json.loads(res_status_init.stdout)
        self.assertEqual(data_after["status"], "ready")
        self.assertTrue(data_after["work_vault"]["exists"])

        # 4. Re-run init via CLI with --allow-nonempty to verify non-overwriting behavior
        res_init2 = subprocess.run(
            [
                sys.executable,
                str(KNOWLEDGE_CLI),
                "init",
                "--work-vault",
                str(self.work_vault),
                "--runtime-dir",
                str(self.runtime_dir),
                "--allow-nonempty",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_init2.returncode, 0)
        init2_data = json.loads(res_init2.stdout)
        self.assertTrue(init2_data["success"])
        self.assertEqual(len(init2_data["created"]), 0)
        self.assertTrue(len(init2_data["skipped"]) > 0)


if __name__ == "__main__":
    unittest.main()
