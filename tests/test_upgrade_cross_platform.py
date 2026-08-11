from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_manager import profiles
from harness_manager.upgrade import upgrade


ROOT = Path(__file__).parents[1]


class CrossPlatformUpgradeTest(unittest.TestCase):
    def install_governed(self, target: Path) -> None:
        profiles.copy_brain(ROOT / ".agent", target / ".agent", profile=profiles.STANDARD)
        record = profiles.profile_record(profiles.STANDARD)
        (target / ".agent/install.json").write_text(json.dumps({
            "schema_version": 1,
            "agentic_stack_version": "0.19.0",
            "abs_target": str(target.resolve()),
            "installed_at": "2026-08-11T00:00:00Z",
            "adapters": {},
            "orchestration": record,
        }))

    def test_governed_profile_upgrade_is_cross_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_governed(target)
            self.assertEqual(upgrade(target, ROOT, yes=True, log=lambda _line: None), 0)
            state = json.loads((target / ".agent/install.json").read_text())
            self.assertEqual(state["orchestration"]["providers"], ["governance", "crg-evidence"])

    def test_upgrade_preserves_local_schedule_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_governed(target)
            path = target / ".agent/memory/orchestration/scheduled-local.json"
            before = path.read_bytes()
            self.assertEqual(upgrade(target, ROOT, yes=True, log=lambda _line: None), 0)
            self.assertEqual(path.read_bytes(), before)

    def test_legacy_provider_tree_requires_explicit_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.install_governed(target)
            state = json.loads((target / ".agent/install.json").read_text())
            state["orchestration"].pop("architecture")
            (target / ".agent/install.json").write_text(json.dumps(state))
            (target / ".agent/runtime/providers/memos-local-plugin").mkdir(parents=True)
            self.assertEqual(upgrade(target, ROOT, yes=True, log=lambda _line: None), 2)


if __name__ == "__main__":
    unittest.main()
