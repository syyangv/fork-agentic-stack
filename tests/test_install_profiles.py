import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from harness_manager import install as install_mod
from harness_manager import cli
from harness_manager import remove as remove_mod
from harness_manager import schema
from harness_manager import state as state_mod
from harness_manager import scheduled_runtime
from harness_manager import upgrade as upgrade_mod


ROOT = Path(__file__).resolve().parents[1]


class InstallationProfileTest(unittest.TestCase):
    def setUp(self):
        self.manifest = schema.validate(ROOT / "adapters" / "claude-code" / "adapter.json")
        self.adapter_dir = ROOT / "adapters" / "claude-code"

    def install(self, target: Path, *, profile: str) -> None:
        install_mod.install(
            manifest=self.manifest,
            target_root=target,
            adapter_dir=self.adapter_dir,
            stack_root=ROOT,
            profile=profile,
            log=lambda _message: None,
        )

    def state(self, target: Path) -> dict:
        return json.loads((target / ".agent" / "install.json").read_text(encoding="utf-8"))

    def upgrade(self, target: Path) -> int:
        return upgrade_mod.upgrade(target, ROOT, yes=True, log=lambda _message: None)

    def agent_files(self, target: Path) -> dict[str, bytes]:
        agent = target / ".agent"
        return {
            path.relative_to(agent).as_posix(): path.read_bytes()
            for path in agent.rglob("*")
            if (
                path.is_file()
                and not path.name.endswith(".lock")
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )
        }

    def run_cli(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "AGENTIC_STACK_ROOT": str(ROOT),
        }
        return subprocess.run(
            [sys.executable, "-m", "harness_manager.cli", *args],
            cwd=cwd, text=True, capture_output=True, env=environment, check=False,
        )

    def test_standard_profile_keeps_memos_capability_but_records_phase8_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")

            orchestration = self.state(target)["orchestration"]
            self.assertEqual(orchestration["profile"], "standard")
            self.assertEqual(orchestration["phase8_quality_gate"], "blocked")
            self.assertEqual(orchestration["memos_capability"], "available")
            self.assertEqual(orchestration["memos_mode"], "off")
            self.assertFalse(orchestration["evolution_enabled"])
            self.assertFalse(orchestration["r7_skill_promoted"])
            self.assertTrue(
                (target / ".agent/memory/orchestration/memos_factory.py").is_file()
            )
            config = json.loads(
                (target / ".agent/memory/orchestration/config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["mode"], "off")

    def test_minimal_profile_omits_memos_and_retains_governance_only_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="minimal")

            orchestration = self.state(target)["orchestration"]
            self.assertEqual(orchestration, {
                "profile": "minimal",
                "phase8_quality_gate": "blocked",
                "governance_only": True,
                "scheduled_runtime": scheduled_runtime.select_runtime().record(),
            })
            self.assertTrue(
                (target / ".agent/memory/orchestration/providers/governance.py").is_file()
            )
            self.assertFalse(
                (target / ".agent/memory/orchestration/memos_factory.py").exists()
            )
            self.assertFalse(
                (target / ".agent/memory/orchestration/providers/memos_local.py").exists()
            )
            self.assertFalse(
                (target / ".agent/memory/orchestration/host_evolution.py").exists()
            )
            self.assertFalse(
                (target / ".agent/memory/orchestration/evolution_eval.py").exists()
            )
            self.assertTrue((target / ".agent/tools/memory_orchestrate.py").is_file())
            self.assertEqual(
                [
                    path.relative_to(target / ".agent").as_posix()
                    for path in (target / ".agent").rglob("*")
                    if "memos" in path.name.lower()
                ],
                [],
            )
            config = json.loads(
                (target / ".agent/memory/orchestration/config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["mode"], "off")

    def test_profile_install_does_not_replace_existing_governance_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")
            config_path = target / ".agent/memory/orchestration/config.json"
            governance_config = {
                "schema": "agentic.memory.config.v1",
                "mode": "off",
                "total_token_budget": 12000,
                "lane_reserves": {
                    "governance": 5000,
                    "behavioral": 4000,
                    "evidence": 3000,
                },
                "project_aliases": {"legacy": "governance-only"},
            }
            config_path.write_text(json.dumps(governance_config), encoding="utf-8")

            self.install(target, profile="standard")

            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), governance_config)

    def test_cli_exposes_minimal_profile_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            with mock.patch.object(cli, "_maybe_run_onboard", return_value=0):
                self.assertEqual(
                    cli.main(["claude-code", str(target), "--profile", "minimal", "--yes"]),
                    0,
                )

            self.assertEqual(self.state(target)["orchestration"]["profile"], "minimal")
            self.assertFalse(
                (target / ".agent/memory/orchestration/memos_factory.py").exists()
            )

    def test_profile_change_is_rejected_before_existing_brain_is_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")
            config_path = target / ".agent/memory/orchestration/config.json"
            original = config_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cannot change installation profile"):
                self.install(target, profile="minimal")

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertTrue(
                (target / ".agent/memory/orchestration/memos_factory.py").is_file()
            )

    def test_minimal_profile_retains_an_off_safe_tool_and_drains_hook_spool(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="minimal")
            tool = target / ".agent/tools/memory_orchestrate.py"
            self.assertTrue(tool.is_file())

            runner = """
import importlib.util
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
agent = root / '.agent'
sys.path.insert(0, str(agent / 'memory'))
from orchestration.contracts import EventEnvelope
from orchestration.identity import derive_project_identity

spec = importlib.util.spec_from_file_location('installed_orchestration_event', agent / 'harness/hooks/orchestration_event.py')
hook = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hook
spec.loader.exec_module(hook)
identity = derive_project_identity(root)
event = EventEnvelope.create(
    idempotency_key='minimal-hook-event', timestamp='2026-07-28T00:00:00Z',
    event_type='task.started', project_id=identity.project_id,
    repo_root=identity.repo_root, revision=None, harness='codex',
    run_id='run_minimal', session_id='session_minimal', actor='user',
    intent='safe lifecycle event', payload={},
)
spool = hook.HookEventSpool(agent)
spool.enqueue(event)
assert hook.drain_spool(spool, hook._batch_subprocess_deliver) == 1
assert spool.pending() == []
print(json.dumps(json.loads(spool.health_file.read_text(encoding='utf-8'))))
"""
            environment = {**os.environ, "AGENTIC_PROJECT_ROOT": str(target)}
            result = subprocess.run(
                [sys.executable, "-c", runner, str(target)],
                text=True, capture_output=True, env=environment, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["pending"], 0)

            retained = [
                tool,
                target / ".agent/tools/memory_review.py",
                target / ".agent/tools/revalidate_memory.py",
                target / ".agent/tools/graduate.py",
                target / ".agent/memory/orchestration/revalidation.py",
            ]
            self.assertTrue(all(path.is_file() for path in retained))
            compiled = subprocess.run(
                [sys.executable, "-m", "py_compile", *map(str, retained)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            for path in retained[:-1]:
                imported = subprocess.run(
                    [sys.executable, str(path), "--help"],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)

    def test_upgrade_keeps_minimal_profile_without_memos_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="minimal")

            self.assertEqual(self.upgrade(target), 0)

            agent = target / ".agent"
            self.assertEqual(self.state(target)["orchestration"]["profile"], "minimal")
            self.assertEqual(
                [path.relative_to(agent).as_posix() for path in agent.rglob("*") if "memos" in path.name.lower()],
                [],
            )
            infrastructure = json.loads((agent / "infrastructure.json").read_text(encoding="utf-8"))
            self.assertFalse(any(str(value).startswith("memos_") for value in infrastructure.get("features", [])))

    def test_upgrade_keeps_standard_profile_and_migrates_valid_unprofiled_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")
            document = self.state(target)
            document.pop("orchestration")
            (target / ".agent/install.json").write_text(json.dumps(document), encoding="utf-8")

            self.assertEqual(self.upgrade(target), 0)

            orchestration = self.state(target)["orchestration"]
            self.assertEqual(orchestration["profile"], "standard")
            self.assertEqual(orchestration["memos_mode"], "off")

    def test_upgrade_fails_before_mutation_for_malformed_profile_or_unsafe_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="minimal")
            document = self.state(target)
            document["orchestration"]["profile"] = "unknown"
            (target / ".agent/install.json").write_text(json.dumps(document), encoding="utf-8")
            before = self.agent_files(target)

            self.assertEqual(self.upgrade(target), 2)
            self.assertEqual(self.agent_files(target), before)

            document.pop("orchestration")
            (target / ".agent/install.json").write_text(json.dumps(document), encoding="utf-8")
            config_path = target / ".agent/memory/orchestration/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["mode"] = "shadow"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            before = self.agent_files(target)

            self.assertEqual(self.upgrade(target), 2)
            self.assertEqual(self.agent_files(target), before)

    def test_upgrade_requires_install_state_for_unprofiled_brains(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            agent = target / ".agent"
            agent.mkdir()
            before = self.agent_files(target)

            self.assertEqual(self.upgrade(target), 2)
            self.assertEqual(self.agent_files(target), before)

    def test_reinstall_rejects_unprofiled_brain_without_standard_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".agent").mkdir()
            before = self.agent_files(target)

            with self.assertRaisesRegex(ValueError, "cannot be safely migrated to standard"):
                self.install(target, profile="standard")
            self.assertEqual(self.agent_files(target), before)

    def test_blocked_quality_gate_rejects_active_config_before_reinstall_or_upgrade(self):
        for profile, mode in (("standard", "shadow"), ("minimal", "assist")):
            with self.subTest(profile=profile, mode=mode), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                self.install(target, profile=profile)
                config_path = target / ".agent/memory/orchestration/config.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["mode"] = mode
                config_path.write_text(json.dumps(config), encoding="utf-8")
                before = self.agent_files(target)

                with self.assertRaisesRegex(ValueError, "Phase 8 quality gate is blocked"):
                    self.install(target, profile=profile)
                self.assertEqual(self.agent_files(target), before)

                self.assertEqual(self.upgrade(target), 2)
                self.assertEqual(self.agent_files(target), before)

    def test_blocked_quality_gate_rejects_enabled_evolution_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")
            document = self.state(target)
            document["orchestration"]["evolution_enabled"] = True
            (target / ".agent/install.json").write_text(json.dumps(document), encoding="utf-8")
            before = self.agent_files(target)

            with self.assertRaisesRegex(ValueError, "evolution is enabled"):
                self.install(target, profile="standard")
            self.assertEqual(self.agent_files(target), before)

            self.assertEqual(self.upgrade(target), 2)
            self.assertEqual(self.agent_files(target), before)

    def test_blocked_quality_gate_rejects_promoted_r7_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")
            document = self.state(target)
            document["orchestration"]["r7_skill_promoted"] = True
            (target / ".agent/install.json").write_text(json.dumps(document), encoding="utf-8")
            before = self.agent_files(target)

            with self.assertRaisesRegex(ValueError, "R7 skill is promoted"):
                self.install(target, profile="standard")
            self.assertEqual(self.agent_files(target), before)

            self.assertEqual(self.upgrade(target), 2)
            self.assertEqual(self.agent_files(target), before)

    def test_last_adapter_removal_preserves_profile_and_governance_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="minimal")
            config_path = target / ".agent/memory/orchestration/config.json"
            governance_config = json.loads(config_path.read_text(encoding="utf-8"))
            profile = dict(self.state(target)["orchestration"])

            self.assertEqual(remove_mod.remove(target, "claude-code", yes=True, log=lambda _message: None), 0)

            document = self.state(target)
            self.assertEqual(document["adapters"], {})
            self.assertEqual(document["orchestration"], profile)
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), governance_config)

    def test_install_scripts_document_explicit_profile_surfaces(self):
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("[ValidateSet('standard', 'minimal')]", powershell)
        self.assertIn("[string]$Profile", powershell)
        self.assertIn("--profile", powershell)
        self.assertIn("--profile standard|minimal", shell)

    def test_minimal_upgrade_is_byte_and_mtime_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="minimal")
            self.assertEqual(self.upgrade(target), 0)
            inventory = target / ".agent/infrastructure.json"
            before = (inventory.read_bytes(), inventory.stat().st_mtime_ns)
            time.sleep(0.01)
            logs: list[str] = []

            self.assertEqual(upgrade_mod.upgrade(target, ROOT, yes=True, log=logs.append), 0)

            self.assertTrue(any("already current" in line for line in logs))
            self.assertEqual((inventory.read_bytes(), inventory.stat().st_mtime_ns), before)

    def test_cli_policy_errors_are_concise_and_profile_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")
            config_path = target / ".agent/memory/orchestration/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["mode"] = "shadow"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            reinstall = self.run_cli(target, "claude-code", str(target), "--yes")
            add = self.run_cli(target, "add", "codex", str(target))
            self.assertEqual(reinstall.returncode, 2)
            self.assertEqual(add.returncode, 2)
            self.assertIn("Phase 8 quality gate is blocked", reinstall.stderr)
            self.assertIn("Phase 8 quality gate is blocked", add.stderr)
            self.assertNotIn("Traceback", reinstall.stderr)
            self.assertNotIn("Traceback", add.stderr)

            for command in (
                ("doctor", "--profile", "minimal"),
                ("upgrade", str(target), "--profile", "minimal", "--yes"),
            ):
                result = self.run_cli(target, *command)
                self.assertEqual(result.returncode, 2)
                self.assertIn("--profile is only supported", result.stderr)

            bare = self.run_cli(target, "--profile", "minimal")
            self.assertEqual(bare.returncode, 2)
            self.assertIn("--profile cannot change an installed project", bare.stderr)

    def test_profiled_quality_gate_is_required_and_runtime_fails_closed(self):
        for gate in (None, "passed"):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                self.install(target, profile="standard")
                document = self.state(target)
                if gate is None:
                    document["orchestration"].pop("phase8_quality_gate")
                else:
                    document["orchestration"]["phase8_quality_gate"] = gate
                (target / ".agent/install.json").write_text(json.dumps(document), encoding="utf-8")
                before = self.agent_files(target)

                runner = """
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

agent = Path(sys.argv[1]) / '.agent'
spec = importlib.util.spec_from_file_location('installed_memory_orchestrate', agent / 'tools/memory_orchestrate.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(module._effective_mode(SimpleNamespace(mode='assist')))
"""
                runtime = subprocess.run(
                    [sys.executable, "-c", runner, str(target)],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(runtime.returncode, 0, runtime.stderr)
                self.assertEqual(runtime.stdout.strip(), "off")

                with self.assertRaisesRegex(ValueError, "Phase 8 quality gate"):
                    self.install(target, profile="standard")
                self.assertEqual(self.upgrade(target), 2)
                self.assertEqual(self.agent_files(target), before)

    def test_atomic_adapter_and_profile_state_rejects_invalid_gate_without_partial_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install(target, profile="standard")
            before = (target / ".agent/install.json").read_bytes()
            entry = {"files_written": ["x"], "files_overwritten": []}
            invalid = {"profile": "standard", "phase8_quality_gate": "passed"}

            with self.assertRaisesRegex(ValueError, "Phase 8 quality gate"):
                state_mod.upsert_adapter_with_profile(target, "synthetic", entry, "test", invalid)
            self.assertEqual((target / ".agent/install.json").read_bytes(), before)

            record = {
                "profile": "standard", "phase8_quality_gate": "blocked",
                "memos_capability": "available", "memos_mode": "off",
                "evolution_enabled": False, "r7_skill_promoted": False,
                "scheduled_runtime": scheduled_runtime.select_runtime().record(),
            }
            state_mod.upsert_adapter_with_profile(target, "synthetic", entry, "test", record)
            document = self.state(target)
            self.assertIn("synthetic", document["adapters"])
            self.assertEqual(document["orchestration"], record)


if __name__ == "__main__":
    unittest.main()
