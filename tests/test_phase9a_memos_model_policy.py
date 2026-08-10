"""Acceptance tests for Phase 9A's no-unapproved-model boundary.

These tests intentionally describe the target policy before the production
implementation changes.  A failing test is evidence that a model-backed path
has not yet been removed or made explicit in the qualification inventory.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agent" / "memory"))

from orchestration.memos_runtime import build_memos_config, memos_model_inventory
from harness_manager.behavioral_export import (
    BehavioralExportError,
    _validate_managed_profile,
)


PROJECT_ID = "0123456789abcdef"
QUALIFICATION_SCRIPT = ROOT / "scripts" / "qualify_memos_zero_egress.py"

# Deliberately limited to the MemOS integration/qualification boundary.  This
# avoids mistaking unrelated CRG metadata or documentation for executable
# model configuration.
MODEL_POLICY_SOURCES = (
    ".agent/memory/orchestration/memos_runtime.py",
    ".agent/memory/orchestration/memos_factory.py",
    ".agent/memory/orchestration/memos_bridge.py",
    ".agent/memory/orchestration/providers/memos_local.py",
    ".agent/memory/orchestration/host_evolution.py",
    ".agent/memory/orchestration/memos_backup.py",
    "harness_manager/memos_install.py",
    "harness_manager/behavioral_export.py",
    "tests/qualification/phase9a_memos_214_offline_benchmark.py",
)


def _qualification_module():
    spec = importlib.util.spec_from_file_location("phase9a_zero_egress_model_policy", QUALIFICATION_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemosModelPolicyTest(unittest.TestCase):
    def test_runtime_defaults_to_model_free_lexical_fts(self) -> None:
        config = build_memos_config(PROJECT_ID)
        self.assertEqual(config["embedding"], {
            "enabled": False,
            "provider": "lexical",
            "engine": "sqlite_fts5",
        })
        self.assertNotIn("model", json.dumps(config).casefold())
        self.assertEqual(config["llm"], {
            "provider": "local_only", "fallbackToHost": False, "maxRetries": 0,
        })
        self.assertEqual(memos_model_inventory(config)["llm"]["route"], "disabled")

    def test_approved_host_profile_inventory_is_not_mislabeled_model_free(self) -> None:
        config = build_memos_config(
            PROJECT_ID, evolution_pilot=True, host_model="gpt-5.4",
        )
        inventory = memos_model_inventory(config)
        self.assertEqual(inventory["retrieval"]["mode"], "lexical")
        self.assertFalse(inventory["embedding"]["enabled"])
        self.assertEqual(inventory["llm"], {
            "route": "approved_claude_codex_host",
            "model": "gpt-5.4",
            "provider_credentials": False,
        })

    def test_export_accepts_only_model_free_lexical_profile(self) -> None:
        config = build_memos_config(PROJECT_ID)
        config["embedding"] = {
            "enabled": False, "provider": "lexical", "engine": "sqlite_fts5",
        }
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            plugin = Path(tmp)
            (plugin / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
            try:
                _validate_managed_profile(plugin)
            except BehavioralExportError as exc:
                self.fail(f"model-free lexical profile was rejected: {exc}")

    def test_provider_credentials_are_rejected_and_never_in_default_config(self) -> None:
        config = build_memos_config(PROJECT_ID)
        rendered = json.dumps(config, sort_keys=True).casefold()
        self.assertNotRegex(rendered, r'api.?key|access.?token|client.?secret|credentials')
        for section, key in (("embedding", "apiKey"), ("llm", "accessToken")):
            candidate = json.loads(json.dumps(config))
            candidate[section][key] = "not-a-real-secret"
            with self.subTest(section=section, key=key), tempfile.TemporaryDirectory(
                dir=Path(tempfile.gettempdir()).resolve()
            ) as tmp:
                plugin = Path(tmp)
                (plugin / "config.yaml").write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(BehavioralExportError):
                    _validate_managed_profile(plugin)

    def test_sources_contain_no_unapproved_model_loader_or_remote_fallback(self) -> None:
        prohibited = (
            "all-minilm", "xenova/", "huggingface.co/", "sentence-transformers",
            "transformers.pipeline", "fallbacktohost\": true", "openai/",
            "cohere/", "voyage/", "ollama/",
        )
        findings: list[str] = []
        for relative in MODEL_POLICY_SOURCES:
            text = (ROOT / relative).read_text(encoding="utf-8").casefold()
            findings.extend(f"{relative}: {needle}" for needle in prohibited if needle in text)
        self.assertEqual(findings, [], "unapproved model paths: " + ", ".join(findings))

    def test_pinned_asset_has_no_huggingface_transformer_loader(self) -> None:
        lock = json.loads((ROOT / "harness_manager/assets/memos-2.0.14/package-lock.lexical.json").read_text())
        packages = lock.get("packages", {})
        self.assertNotIn("node_modules/@huggingface/transformers", packages)

    def test_qualification_reports_complete_model_inventory(self) -> None:
        module = _qualification_module()
        source = QUALIFICATION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("model_inventory", source)
        self.assertTrue(hasattr(module, "MODEL_INVENTORY"))
        self.assertEqual(module.MODEL_INVENTORY, {
            "retrieval": {"mode": "lexical", "engine": "sqlite_fts5", "model": None},
            "embedding": {"enabled": False, "provider_credentials": False},
            "memos_llm": {"mode": "disabled", "provider_credentials": False},
            "host_evolution": {"mode": "disabled", "route": "approved_host_only"},
            "remote_fallback": False,
        })

    def test_legacy_model_profile_is_only_an_isolated_noncompliant_seed(self) -> None:
        module = _qualification_module()
        legacy = module._legacy_2010_config()
        self.assertEqual(legacy["embedding"]["provider"], "local")
        source = QUALIFICATION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("config_value=_legacy_2010_config()", source)
        self.assertIn("(fresh_result, copied_result, restored_result)", source)

        migration_path = ROOT / "tests/qualification/phase9a_memos_214_migration_rehearsal.py"
        migration = importlib.util.spec_from_file_location(
            "phase9a_migration_model_policy", migration_path,
        )
        assert migration is not None and migration.loader is not None
        migration_module = importlib.util.module_from_spec(migration)
        migration.loader.exec_module(migration_module)
        rollback = migration_module.legacy_2010_config()
        self.assertFalse(rollback["algorithm"]["capture"]["embedTraces"])


if __name__ == "__main__":
    unittest.main()
