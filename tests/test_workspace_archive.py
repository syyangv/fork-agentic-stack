#!/usr/bin/env python3
"""
Guards for .agent/memory/archive.py.

The dream cycle archives a stale WORKSPACE.md so tasks don't span
nights. It used to do that with a bare shutil.move(), which left
nothing behind — but WORKSPACE.md is tracked in git and every adapter's
instructions tell agents to "update .agent/memory/working/WORKSPACE.md
as you work". So after two idle days the file simply vanished, showing
up as a phantom `git status` deletion and breaking
test_copilot_cli_adapter_roundtrip, which asserts the path exists.

Archiving is meant to clear stale state, not remove the place state
goes. These tests pin that distinction.

Run from the agentic-stack repo root:

    python3 -m pytest tests/test_workspace_archive.py
"""

import importlib.util
import os
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PY = REPO_ROOT / ".agent" / "memory" / "archive.py"


def _load_archive_module():
    """Load archive.py by path — .agent/memory is not an importable package."""
    spec = importlib.util.spec_from_file_location("agent_memory_archive", ARCHIVE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archive = _load_archive_module()


@pytest.fixture
def dirs(tmp_path):
    working = tmp_path / "working"
    snapshots = tmp_path / "episodic" / "snapshots"
    working.mkdir(parents=True)
    return working, snapshots


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_stale_workspace_is_archived_but_not_left_missing():
    """The actual regression: the file must still exist afterwards."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        working = Path(tmp) / "working"
        snapshots = Path(tmp) / "snapshots"
        working.mkdir(parents=True)
        ws = working / "WORKSPACE.md"
        ws.write_text("# stale task state\n")
        _age(ws, archive.STALE_DAYS + 1)

        assert archive.archive_stale_workspace(str(working), str(snapshots)) is True
        assert ws.exists(), "WORKSPACE.md was archived into a hole"


def test_archived_copy_keeps_the_original_content(dirs):
    working, snapshots = dirs
    ws = working / "WORKSPACE.md"
    original = "# stale task state\nhypothesis: the cache is cold\n"
    ws.write_text(original)
    _age(ws, archive.STALE_DAYS + 1)

    archive.archive_stale_workspace(str(working), str(snapshots))

    copies = list(snapshots.glob("workspace_*.md"))
    assert len(copies) == 1
    assert copies[0].read_text() == original


def test_replacement_is_a_fresh_template_not_the_old_state(dirs):
    working, snapshots = dirs
    ws = working / "WORKSPACE.md"
    ws.write_text("# stale task state\nsecret: leftover context\n")
    _age(ws, archive.STALE_DAYS + 1)

    archive.archive_stale_workspace(str(working), str(snapshots))

    fresh = ws.read_text()
    assert "leftover context" not in fresh
    for heading in ("## Current task", "## Open files", "## Next step"):
        assert heading in fresh


def test_recent_workspace_is_left_alone(dirs):
    working, snapshots = dirs
    ws = working / "WORKSPACE.md"
    live = "# today's work\nmid-task, do not touch\n"
    ws.write_text(live)

    assert archive.archive_stale_workspace(str(working), str(snapshots)) is False
    assert ws.read_text() == live, "clobbered a live workspace"
    assert not snapshots.exists()


def test_missing_workspace_is_reseeded(dirs):
    """Self-heal for projects the old shutil.move() already emptied."""
    working, snapshots = dirs
    ws = working / "WORKSPACE.md"
    assert not ws.exists()

    assert archive.archive_stale_workspace(str(working), str(snapshots)) is False
    assert ws.exists()
    assert "## Current task" in ws.read_text()


def test_reseeded_workspace_is_not_immediately_stale(dirs):
    """Otherwise the next dream cycle would archive the empty template."""
    working, snapshots = dirs
    ws = working / "WORKSPACE.md"
    ws.write_text("# stale\n")
    _age(ws, archive.STALE_DAYS + 1)

    archive.archive_stale_workspace(str(working), str(snapshots))
    # Second cycle, immediately after: nothing left to archive.
    assert archive.archive_stale_workspace(str(working), str(snapshots)) is False
    assert len(list(snapshots.glob("workspace_*.md"))) == 1
