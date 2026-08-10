import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from harness_manager import install as install_mod
from harness_manager import profiles
from harness_manager import schema
from harness_manager import upgrade as upgrade_mod


ROOT = Path(__file__).resolve().parents[1]


class CrossPlatformUpgradeTest(unittest.TestCase):
    def install_standard(self, target: Path) -> None:
        manifest = schema.validate(ROOT / "adapters/claude-code/adapter.json")
        install_mod.install(
            manifest=manifest,
            target_root=target,
            adapter_dir=ROOT / "adapters/claude-code",
            stack_root=ROOT,
            profile="standard",
            log=lambda _message: None,
        )

    def portable_backend(self):
        if os.name == "nt":
            return nullcontext()
        return mock.patch.multiple(
            upgrade_mod,
            _descriptor_relative_supported=mock.Mock(return_value=False),
        )

    def add_legacy_memos_configs(self, target: Path):
        project = "0123456789abcdef"
        data = target / ".agent/runtime/memos"
        pilots = data / "pilot-configs"
        pilots.mkdir(parents=True)
        manifest = pilots / f"{project}.json"
        manifest.write_text(json.dumps({
            "schema": "agentic.memory.evolution-pilot.v2", "enabled": False,
            "project_id": project, "repo_root": str(target), "provider": "claude_opus",
            "model": "opus", "daily_caps": {"policy": 5, "world_model": 2, "skill": 2, "other": 50},
            "min_distinct_episodes": 3, "timeout_seconds": 60,
        }))
        manifest.chmod(0o600)
        legacy = {
            "version": 1,
            "embedding": {"provider": "local", "model": "Xenova/all-MiniLM-L6-v2"},
            "llm": {"provider": "local_only", "fallbackToHost": False, "maxRetries": 0},
            "algorithm": {"capture": {"embedTraces": False}},
            "hub": {"enabled": False}, "telemetry": {"enabled": False},
        }
        active = data / project / "profiles" / project / "memos-plugin" / "config.yaml"
        rollback = data / f".{project}.rollback-{'a' * 32}" / "profiles" / project / "memos-plugin" / "config.yaml"
        for path in (active, rollback):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(legacy)); path.chmod(0o600)
        unrelated = data / "foreign/profiles" / project / "memos-plugin/config.yaml"
        unrelated.parent.mkdir(parents=True); unrelated.write_text(json.dumps(legacy))
        return active, rollback, unrelated

    def test_upgrade_transaction_migrates_only_owned_memos_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_standard(target)
            active, rollback, unrelated = self.add_legacy_memos_configs(target)
            result = upgrade_mod.upgrade(target, ROOT, yes=True, log=lambda _message: None)
            self.assertEqual(result, 0)
            for path in (active, rollback):
                self.assertEqual(json.loads(path.read_text())["embedding"]["provider"], "lexical")
            self.assertEqual(json.loads(unrelated.read_text())["embedding"]["provider"], "local")
            record = target / ".agent/runtime/memos/migrations/current.json"
            self.assertTrue(json.loads(record.read_text())["committed"])

    def test_upgrade_failure_after_migration_restores_configs_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_standard(target)
            active, rollback, _unrelated = self.add_legacy_memos_configs(target)
            before = {path: path.read_bytes() for path in (active, rollback)}
            real_commit = upgrade_mod._UpgradeRollback.commit
            calls = 0
            def fail_once(transaction):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected post-migration commit failure")
                return real_commit(transaction)
            with mock.patch.object(upgrade_mod._UpgradeRollback, "commit", fail_once):
                result = upgrade_mod.upgrade(target, ROOT, yes=True, log=lambda _message: None)
            self.assertEqual(result, 2)
            for path, raw in before.items():
                self.assertEqual(path.read_bytes(), raw)
            self.assertFalse((target / ".agent/runtime/memos/migrations/current.json").exists())
            self.assertFalse((target / ".agent/.upgrade-transaction.json").exists())

    def test_upgrade_uses_portable_backend_without_dir_fd_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_standard(target)
            deployed = target / ".agent/infrastructure.json"
            deployed.write_text("stale\n", encoding="utf-8")
            profile_backend = (
                nullcontext()
                if os.name == "nt"
                else mock.patch.object(
                    profiles, "_descriptor_relative_supported", return_value=False,
                )
            )
            with self.portable_backend(), profile_backend:
                result = upgrade_mod.upgrade(
                    target, ROOT, yes=True, log=lambda _message: None,
                )

            self.assertEqual(result, 0)
            self.assertNotEqual(deployed.read_text(encoding="utf-8"), "stale\n")

    def test_phase2_profile_migration_uses_portable_validation_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_standard(target)
            install_state = target / ".agent/install.json"
            document = json.loads(install_state.read_text(encoding="utf-8"))
            document.pop("orchestration")
            install_state.write_text(json.dumps(document), encoding="utf-8")
            for relative in profiles.minimal_omitted_paths():
                path = target / ".agent" / relative
                if path.exists():
                    path.unlink()
            inventory = {
                "schema_version": 1,
                "stack_version": profiles._PHASE2_STACK_VERSION,
                "orchestration_phase": 2,
                "features": list(profiles._PHASE2_FEATURES),
            }
            (target / ".agent/infrastructure.json").write_text(
                json.dumps(inventory), encoding="utf-8",
            )
            (target / ".agent/memory/orchestration/config.json").write_text(
                json.dumps(profiles._PHASE2_CONFIG), encoding="utf-8",
            )
            profile_backend = (
                nullcontext()
                if os.name == "nt"
                else mock.patch.object(
                    profiles, "_descriptor_relative_supported", return_value=False,
                )
            )
            with self.portable_backend(), profile_backend:
                result = upgrade_mod.upgrade(
                    target, ROOT, yes=True, log=lambda _message: None,
                )

            self.assertEqual(result, 0)
            migrated = json.loads(install_state.read_text(encoding="utf-8"))
            self.assertEqual(migrated["orchestration"]["profile"], "standard")

    def test_portable_recovery_journal_restores_before_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_standard(target)
            agent = upgrade_mod._safe_lexical_absolute(target) / ".agent"
            relative = Path("infrastructure.json")
            deployed = agent / relative
            before = deployed.read_bytes()
            identity = upgrade_mod._portable_root_identity(agent)
            rollback = upgrade_mod._UpgradeRollback.capture(
                root_fd=None,
                dst_agent=agent,
                portable_root_identity=identity,
                relatives=[relative],
            )
            rollback.persist()
            deployed.write_bytes(b"interrupted\n")

            self.assertTrue(
                upgrade_mod._UpgradeRollback.recover_if_present(
                    root_fd=None,
                    dst_agent=agent,
                    portable_root_identity=identity,
                )
            )
            self.assertEqual(deployed.read_bytes(), before)
            self.assertFalse((agent / ".upgrade-transaction.json").exists())

    @unittest.skipUnless(os.name == "nt", "requires native Windows junctions")
    def test_windows_junction_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            self.install_standard(target)
            junction = target / ".agent/memory/orchestration"
            shutil.rmtree(junction)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr or created.stdout)

            result = upgrade_mod.upgrade(
                target, ROOT, yes=True, log=lambda _message: None,
            )

            self.assertEqual(result, 2)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")


if __name__ == "__main__":
    unittest.main()
