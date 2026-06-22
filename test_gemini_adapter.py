import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GeminiAdapterTests(unittest.TestCase):
    def test_manifest_wires_expected_files_and_skills(self):
        manifest = json.loads((ROOT / "adapters" / "gemini" / "adapter.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "gemini")
        dsts = [entry["dst"] for entry in manifest["files"]]
        self.assertIn("GEMINI.md", dsts)
        self.assertIn(".gemini/settings.json", dsts)
        self.assertEqual(manifest["skills_link"]["dst"], ".gemini/skills")
        self.assertEqual(manifest["skills_link"]["fallback"], "copy_with_merge")

    def test_settings_wire_after_tool_and_session_end_hooks(self):
        settings = json.loads((ROOT / "adapters" / "gemini" / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(settings["hooksConfig"]["enabled"])
        self.assertTrue(settings["skills"]["enabled"])
        after_tool = settings["hooks"]["AfterTool"][0]
        self.assertEqual(after_tool["matcher"], "run_shell_command|replace|write_file|write_todos")
        after_tool_command = after_tool["hooks"][0]["command"]
        self.assertIn(".agent/harness/hooks/gemini_post_tool.py", after_tool_command)

        session_end = settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        self.assertIn(".agent/memory/auto_dream.py", session_end)

    def test_doctor_detects_gemini_settings(self):
        doctor = (ROOT / "harness_manager" / "doctor.py").read_text(encoding="utf-8")
        self.assertIn('"gemini"', doctor)
        self.assertIn(".gemini/settings.json", doctor)

    def test_gemini_post_tool_normalizes_payload_shapes(self):
        module = load_module(
            "gemini_post_tool",
            ROOT / ".agent" / "harness" / "hooks" / "gemini_post_tool.py",
        )

        self.assertEqual(module._canonical_tool_name("run_shell_command"), "Bash")

        todos = module._normalize_tool_input(
            "write_todos",
            {"todos": [{"description": "ship it", "status": "in_progress"}]},
        )
        self.assertEqual(todos["todos"][0]["content"], "ship it")

        shell = module._normalize_tool_response(
            "run_shell_command",
            {"returnDisplay": "Stdout: ok\nStderr: (empty)\nExit Code: 0"},
        )
        self.assertEqual(shell["exit_code"], 0)
        self.assertEqual(shell["output"], "ok")

    def test_copy_only_skills_mirror_is_not_a_symlink(self):
        import tempfile

        from harness_manager import install as install_mod, schema as schema_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = schema_mod.validate(ROOT / "adapters" / "gemini" / "adapter.json")
            install_mod.install(
                manifest=manifest,
                target_root=root,
                adapter_dir=ROOT / "adapters" / "gemini",
                stack_root=ROOT,
                log=lambda _msg: None,
            )
            skills_dir = root / ".gemini" / "skills"
            self.assertTrue(skills_dir.is_dir())
            self.assertFalse(skills_dir.is_symlink())

    def test_preexisting_gemini_local_skills_are_preserved(self):
        import tempfile

        from harness_manager import install as install_mod, schema as schema_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing_skill = root / ".gemini" / "skills" / "local-only"
            existing_skill.mkdir(parents=True)
            (existing_skill / "SKILL.md").write_text("---\nname: local-only\n---\n", encoding="utf-8")

            manifest = schema_mod.validate(ROOT / "adapters" / "gemini" / "adapter.json")
            install_mod.install(
                manifest=manifest,
                target_root=root,
                adapter_dir=ROOT / "adapters" / "gemini",
                stack_root=ROOT,
                log=lambda _msg: None,
            )

            self.assertTrue((root / ".gemini" / "skills" / "local-only" / "SKILL.md").is_file())
            self.assertTrue((root / ".gemini" / "skills" / "debug-investigator" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
