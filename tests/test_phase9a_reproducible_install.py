from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/qualification/phase9a_memos_214_reproducible_install.py"
spec = importlib.util.spec_from_file_location("phase9a_reproducible_install", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _fixture(code_root: Path) -> Path:
    plugin = code_root / "memos-local-plugin/2.0.14"
    package = plugin / "node_modules/@memtensor/memos-local-plugin"
    package.mkdir(parents=True)
    (plugin / "node_modules/@huggingface").mkdir()
    (plugin / ".agentic-stack-files.json").write_text("fixture")
    (plugin / "package-lock.json").write_text("fixture")
    (package / "package.json").write_text("fixture")
    (plugin / ".agentic-stack-install.json").write_text(json.dumps({
        "distribution": "agentic-stack-memos-2.0.14-lexical.1",
        "files_manifest_sha256": module.EXPECTED_MANIFEST_SHA256,
        "version": "2.0.14",
    }))
    return plugin


def test_assessment_requires_exact_reproducible_distribution(tmp_path: Path) -> None:
    plugin = _fixture(tmp_path)
    hashes = iter((
        module.EXPECTED_MANIFEST_SHA256,
        module.EXPECTED_LOCK_SHA256,
        module.EXPECTED_PACKAGE_SHA256,
    ))
    with patch.object(module, "validate_installed_plugin", return_value=plugin), \
         patch.object(module, "_sha256", side_effect=lambda _: next(hashes)):
        result = module.assess_install(tmp_path)
    assert result["manifest_sha256"] == module.EXPECTED_MANIFEST_SHA256
    assert all(result["checks"].values())


def test_runner_demands_two_fresh_installs_and_equal_manifests(tmp_path: Path) -> None:
    class Result:
        already_installed = False
        artifact_sha1 = "32639d241918c7da8d536e52eac7e0a7c42c312e"

    assessment = {
        "checks": {"installed_tree_valid": True},
        "distribution": "agentic-stack-memos-2.0.14-lexical.1",
        "lock_sha256": module.EXPECTED_LOCK_SHA256,
        "manifest_sha256": module.EXPECTED_MANIFEST_SHA256,
        "package_sha256": module.EXPECTED_PACKAGE_SHA256,
        "version": "2.0.14",
    }
    with patch.object(module, "install_verified_tarball", return_value=Result()) as install, \
         patch.object(module, "assess_install", return_value=assessment):
        evidence = module.qualify(tmp_path / "plugin.tgz", tmp_path / "work")
    assert install.call_count == 2
    assert evidence["passed"] is True
    assert evidence["modes"]["r8_run"] is False
