#!/usr/bin/env python3
"""
Regression guards for the Zed adapter.

Two defects were shipped and caught in review; both are cheap to reintroduce
and neither was caught by the existing suite:

  1. `.rules` installed with merge_policy `overwrite` silently destroyed a
     user-authored `.rules`. Note that install.py defaults to `overwrite`
     when merge_policy is absent, so *deleting* the key regresses it too.
  2. `.rules` registered as a `strong` detection signal made
     legacy_unregistered_adapters() report a phantom zed install in any
     brain-present project that happened to have an unrelated `.rules`.

Run from the agentic-stack repo root:

    python3 -m pytest tests/test_zed_adapter.py
"""

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from harness_manager import doctor, install as install_mod, state  # noqa: E402

ADAPTER_DIR = REPO_ROOT / "adapters" / "zed"


def _manifest() -> dict:
    return json.loads((ADAPTER_DIR / "adapter.json").read_text())


def test_rules_is_never_installed_with_a_destructive_policy():
    entries = [f for f in _manifest()["files"] if f.get("dst") == ".rules"]
    assert entries, "manifest has no .rules entry"
    # Explicit, not inherited: install.py defaults a missing key to overwrite.
    assert entries[0].get("merge_policy") == "merge_or_alert"


def test_rules_is_a_weak_detection_signal():
    signals = dict(doctor.DETECT_SIGNALS["zed"])
    assert signals[".rules"] == "weak", (
        ".rules is a generic root filename; a strong signal makes "
        "legacy_unregistered_adapters() claim a zed install that isn't there"
    )


def test_preexisting_rules_file_survives_install():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        original = "# my own project rules\ndo the thing\n"
        (target / ".rules").write_text(original)

        entry = install_mod.install(
            _manifest(), target, ADAPTER_DIR, REPO_ROOT, log=lambda _m: None
        )

        assert (target / ".rules").read_text() == original
        assert ".rules" in entry.get("files_alerted", [])


def test_clean_project_gets_the_rules_file():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)

        entry = install_mod.install(
            _manifest(), target, ADAPTER_DIR, REPO_ROOT, log=lambda _m: None
        )

        assert ".rules" in entry.get("files_written", [])
        assert ".agent/" in (target / ".rules").read_text()


def test_weak_signal_alone_does_not_imply_a_legacy_zed_install():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        for sub in ("memory", "skills", "protocols"):
            (target / ".agent" / sub).mkdir(parents=True)
        (target / ".rules").write_text("unrelated tooling config\n")

        assert "zed" not in state.legacy_unregistered_adapters(target)
