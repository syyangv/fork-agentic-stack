import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from harness_manager import install as install_mod
from harness_manager import schema
from harness_manager import upgrade as upgrade_mod


ROOT = Path(__file__).resolve().parents[1]


class CrossPlatformUpgradeTest(unittest.TestCase):
    def test_upgrade_uses_portable_backend_without_dir_fd_support(self):
        manifest = schema.validate(ROOT / "adapters/claude-code/adapter.json")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            install_mod.install(
                manifest=manifest,
                target_root=target,
                adapter_dir=ROOT / "adapters/claude-code",
                stack_root=ROOT,
                profile="standard",
                log=lambda _message: None,
            )
            deployed = target / ".agent/infrastructure.json"
            deployed.write_text("stale\n", encoding="utf-8")
            portable_backend = (
                nullcontext()
                if os.name == "nt"
                else mock.patch.object(
                    upgrade_mod, "_descriptor_relative_supported",
                    return_value=False,
                )
            )
            with portable_backend:
                result = upgrade_mod.upgrade(
                    target, ROOT, yes=True, log=lambda _message: None,
                )

            self.assertEqual(result, 0)
            self.assertNotEqual(deployed.read_text(encoding="utf-8"), "stale\n")


if __name__ == "__main__":
    unittest.main()
