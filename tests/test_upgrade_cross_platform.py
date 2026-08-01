import json
import os
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


if __name__ == "__main__":
    unittest.main()
