#!/usr/bin/env python3
"""
Guards for doctor.DETECT_SIGNALS.

The table answers "is this adapter installed here?" without trusting
install.json, which is what makes pre-v0.9 migration possible. Two
consumers read it, and both act on STRONG signals only:

  - state.legacy_unregistered_adapters() refuses to proceed on a
    pre-v0.9 project when a strong signal matches
  - cli.py pre-checks onboarding boxes from strong signals, where a
    false positive means acting on a file we do not own

So a false "strong" is not a cosmetic error. Three defects shipped in
this table and none were caught by the suite:

  1. `gemini` was declared twice; the second dict literal silently
     discarded the first, dropping .gemini/settings.json from
     detection entirely. Python collapses duplicate keys at parse
     time, so no runtime assertion can see this — it needs the AST.
  2. `codex` was strong on `.agent/skills`, which is the shared brain
     rather than anything codex installs, so every brain-present
     project was reported as a legacy codex install.
  3. `.claude/settings.json`, `opencode.json` and `.windsurfrules`
     were strong despite being authored by users and other tools.

Run from the agentic-stack repo root:

    python3 -m pytest tests/test_detection_signals.py
"""

import ast
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from harness_manager import doctor, state  # noqa: E402

DOCTOR_SRC = REPO_ROOT / "harness_manager" / "doctor.py"


def _signal_table_literal() -> ast.Dict:
    """The DETECT_SIGNALS dict literal, straight from the source AST."""
    tree = ast.parse(DOCTOR_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DETECT_SIGNALS"
            for t in node.targets
        ):
            assert isinstance(node.value, ast.Dict)
            return node.value
    pytest.fail("DETECT_SIGNALS assignment not found in doctor.py")


def test_no_adapter_is_declared_twice_in_the_source():
    """Catches the defect a runtime check structurally cannot see."""
    keys = [k.value for k in _signal_table_literal().keys]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicates, (
        f"adapter(s) {duplicates} declared more than once in DETECT_SIGNALS; "
        f"the later literal silently discards the earlier one"
    )


def test_signal_table_satisfies_the_strong_signal_contract():
    doctor.assert_signals_consistent()


def test_gemini_settings_json_is_still_detected():
    """The discarded duplicate block dropped this path entirely."""
    paths = dict(doctor.DETECT_SIGNALS["gemini"])
    assert ".gemini/settings.json" in paths
    assert ".gemini/skills" in paths


def test_every_shipped_adapter_has_detection_signals():
    """An adapter with no signals is invisible to legacy migration."""
    shipped = {
        p.name
        for p in (REPO_ROOT / "adapters").iterdir()
        if p.is_dir() and not p.name.startswith("_")
    }
    assert shipped - set(doctor.DETECT_SIGNALS) == set()


def test_signals_reference_only_shipped_adapters():
    shipped = {p.name for p in (REPO_ROOT / "adapters").iterdir() if p.is_dir()}
    assert set(doctor.DETECT_SIGNALS) - shipped == set()


# --- behavioural: the false positives the demotions remove -----------------


def _brain_project(root: Path) -> None:
    for sub in ("memory", "skills", "protocols"):
        (root / ".agent" / sub).mkdir(parents=True)


def test_bare_brain_is_not_a_legacy_codex_install():
    """`.agent/skills` exists in every brain-present project."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        _brain_project(target)
        assert "codex" not in state.legacy_unregistered_adapters(target)


def test_user_authored_configs_do_not_imply_an_install():
    """Files the user or harness vendor writes prove nothing about us."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        _brain_project(target)
        (target / ".claude").mkdir()
        (target / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
        (target / "opencode.json").write_text("{}\n")
        (target / ".windsurfrules").write_text("my own rules\n")

        detected = state.legacy_unregistered_adapters(target)
        assert "claude-code" not in detected
        assert "opencode" not in detected
        assert "windsurf" not in detected


def test_adapter_owned_path_still_detects():
    """The demotions must not make detection useless where it is honest."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        _brain_project(target)
        rule = target / ".windsurf" / "rules"
        rule.mkdir(parents=True)
        (rule / "agentic-stack.md").write_text("brain wiring\n")

        assert "windsurf" in state.legacy_unregistered_adapters(target)
