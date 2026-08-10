#!/usr/bin/env python3
"""Qualify two independent installs of the pinned lexical MemOS distribution."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness_manager.memos_install import (
    MEMOS_DISTRIBUTION,
    MEMOS_PLUGIN_VERSION,
    install_verified_tarball,
    validate_installed_plugin,
)

EXPECTED_MANIFEST_SHA256 = "dc0aae1417698ed4343895b292fb2f6ac1bcef4820eff6eb46875405b1ed73d9"
EXPECTED_LOCK_SHA256 = "acb61ce0d0806fae9fb155cc1fa18cccb8275ffa5f27a0857567f3973f160f92"
EXPECTED_PACKAGE_SHA256 = "1b6349dcc3fac8cbc27962a00c35b5abbab73a6166c6d28d73db6de55f97a708"
REMOVED_LOADERS = (
    "core/embedding/providers/local.ts",
    "dist/core/embedding/providers/local.js",
    "dist/core/embedding/providers/local.d.ts",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assess_install(code_root: Path) -> dict[str, Any]:
    plugin_dir = validate_installed_plugin(code_root)
    package_dir = plugin_dir / "node_modules/@memtensor/memos-local-plugin"
    marker = json.loads((plugin_dir / ".agentic-stack-install.json").read_text())
    manifest_sha = _sha256(plugin_dir / ".agentic-stack-files.json")
    lock_sha = _sha256(plugin_dir / "package-lock.json")
    package_sha = _sha256(package_dir / "package.json")
    checks = {
        "installed_tree_valid": True,
        "distribution_marker": marker.get("distribution") == MEMOS_DISTRIBUTION,
        "manifest_marker_matches": marker.get("files_manifest_sha256") == manifest_sha,
        "manifest_sha256": manifest_sha == EXPECTED_MANIFEST_SHA256,
        "lock_sha256": lock_sha == EXPECTED_LOCK_SHA256,
        "package_sha256": package_sha == EXPECTED_PACKAGE_SHA256,
        "loader_files_absent": all(not (package_dir / path).exists() for path in REMOVED_LOADERS),
        "transformers_absent": not (plugin_dir / "node_modules/@huggingface/transformers").exists(),
    }
    if not all(checks.values()):
        failed = sorted(key for key, value in checks.items() if not value)
        raise RuntimeError(f"reproducible install checks failed: {', '.join(failed)}")
    return {
        "checks": checks,
        "distribution": marker["distribution"],
        "lock_sha256": lock_sha,
        "manifest_sha256": manifest_sha,
        "package_sha256": package_sha,
        "version": marker["version"],
    }


def qualify(tarball: Path, work_root: Path) -> dict[str, Any]:
    runs = []
    for index in range(2):
        code_root = work_root / f"install-{index}"
        result = install_verified_tarball(tarball, code_root)
        if result.already_installed:
            raise RuntimeError("qualification requires two fresh independent installs")
        runs.append({"run": index, **assess_install(code_root)})
    if runs[0]["manifest_sha256"] != runs[1]["manifest_sha256"]:
        raise RuntimeError("independent install manifests differ")
    return {
        "schema": "agentic.memory.phase9a-reproducible-install.v1",
        "artifact": {"sha1": result.artifact_sha1, "version": MEMOS_PLUGIN_VERSION},
        "deployed_state": "unchanged/off",
        "modes": {"assist": False, "evolution": False, "r8_run": False},
        "passed": True,
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.work_root.exists():
        raise SystemExit("work root must not already exist")
    args.work_root.mkdir(parents=True)
    evidence = qualify(args.artifact, args.work_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
