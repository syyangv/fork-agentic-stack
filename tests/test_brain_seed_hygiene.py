#!/usr/bin/env python3
"""
Guards for profiles.copy_brain() shipping a *fresh* brain.

copy_brain's docstring has always promised "a fresh portable brain", but the
implementation was a bare shutil.copytree of this repo's own .agent/, with
__pycache__ as the only exclusion. So the brain every user installed was
whatever this working tree happened to hold when the release was cut:

  - working/WORKSPACE.md, carrying this project's live task state (on master,
    Phase 9 gating notes naming internal file paths and authorization status)
  - episodic/AGENT_LEARNINGS.jsonl, carrying this repo's action log including
    internal pilot runs
  - working/REVIEW_QUEUE.md, carrying whatever was mid-review

That is one repo's task state installed into user projects as if it were
their own, and it grows the longer a maintainer works before packaging.

The fix is to *seed* fresh state explicitly rather than hope the tree is
clean. The distinction these tests pin: volatile runtime state is reset,
curated seed knowledge (graduated candidates, semantic lessons) survives.

Run from the agentic-stack repo root:

    python3 -m pytest tests/test_brain_seed_hygiene.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_manager import profiles

ROOT = Path(__file__).resolve().parents[1]

# Text planted in the source brain; a leak is this string surviving the copy.
LEAKED = "PHASE 9 GATING - internal task state that must not ship"


def _source_brain(root: Path) -> Path:
    """A source brain dirtied the way a maintainer's tree gets dirtied."""
    agent = root / ".agent"

    working = agent / "memory" / "working"
    working.mkdir(parents=True)
    (working / "WORKSPACE.md").write_text(
        f"# Workspace\n\n## Current task\n- {LEAKED}\n", encoding="utf-8"
    )
    (working / "REVIEW_QUEUE.md").write_text(
        f"# Review Queue\n\nPending: 4\n- {LEAKED}\n", encoding="utf-8"
    )

    episodic = agent / "memory" / "episodic"
    episodic.mkdir(parents=True)
    (episodic / "AGENT_LEARNINGS.jsonl").write_text(
        json.dumps({"skill": "phase8-opus-pilot", "detail": LEAKED}) + "\n",
        encoding="utf-8",
    )
    snapshots = episodic / "snapshots"
    snapshots.mkdir()
    (snapshots / "workspace_2026-08-05.md").write_text(LEAKED, encoding="utf-8")

    candidates = agent / "memory" / "candidates"
    graduated = candidates / "graduated"
    graduated.mkdir(parents=True)
    (graduated / "2647c12dc81d.json").write_text(
        json.dumps({"lesson": "vetted and curated"}), encoding="utf-8"
    )
    (candidates / "staged_abc123.json").write_text(
        json.dumps({"lesson": LEAKED}), encoding="utf-8"
    )
    rejected = candidates / "rejected"
    rejected.mkdir()
    (rejected / "nope.json").write_text(json.dumps({"lesson": LEAKED}), encoding="utf-8")

    semantic = agent / "memory" / "semantic"
    semantic.mkdir(parents=True)
    (semantic / "lessons.jsonl").write_text(
        json.dumps({"lesson": "curated seed knowledge"}) + "\n", encoding="utf-8"
    )

    orchestration = agent / "memory" / "orchestration"
    orchestration.mkdir(parents=True)
    # Required by ensure_local_schedule_config(), which copy_brain calls.
    (orchestration / "scheduled-local.default.json").write_text(
        json.dumps(
            {
                "schema": "agentic.memory.scheduled-local.v1",
                "obsidian_path": None,
                "notification": "disabled",
                "maintenance_schedule": {"hour": 3, "minute": 0},
                "review_schedule": {"hour": 9, "minute": 0},
                "review_server_host": "127.0.0.1",
                "review_server_port": 48999,
            }
        ),
        encoding="utf-8",
    )
    (agent / "skills").mkdir()
    (agent / "skills" / "_manifest.jsonl").write_text(
        json.dumps({"skill": "shipped"}) + "\n", encoding="utf-8"
    )
    (agent / "infrastructure.json").write_text(
        json.dumps({"schema": "agentic.infrastructure.v1", "capabilities": []}),
        encoding="utf-8",
    )
    return agent


class BrainSeedHygieneTest(unittest.TestCase):
    def _install(self, profile: str = profiles.STANDARD):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = _source_brain(root / "src")
        destination = root / "installed" / ".agent"
        destination.parent.mkdir(parents=True)
        profiles.copy_brain(source, destination, profile=profile)
        return source, destination

    # --- the regression --------------------------------------------------

    def test_installed_brain_carries_no_source_task_state(self):
        """The whole point: nothing from the maintainer's tree survives."""
        _, destination = self._install()
        leaks = [
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file() and LEAKED in path.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(leaks, [], f"source task state leaked into install: {leaks}")

    def test_workspace_is_the_fresh_template(self):
        _, destination = self._install()
        workspace = (destination / "memory" / "working" / "WORKSPACE.md").read_text()
        self.assertNotIn(LEAKED, workspace)
        for heading in ("## Current task", "## Open files", "## Next step"):
            self.assertIn(heading, workspace)

    def test_review_queue_is_empty(self):
        _, destination = self._install()
        queue = (destination / "memory" / "working" / "REVIEW_QUEUE.md").read_text()
        self.assertNotIn("Pending: 4", queue)
        self.assertIn("_No pending candidates._", queue)

    def test_episodic_log_starts_empty(self):
        _, destination = self._install()
        log = destination / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
        self.assertEqual(log.read_text(), "")

    # --- reset, not delete -----------------------------------------------

    def test_volatile_files_exist_even_when_source_lacks_them(self):
        """Adapters instruct agents to update WORKSPACE.md; it must exist."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = _source_brain(root / "src")
        (source / "memory" / "working" / "WORKSPACE.md").unlink()
        (source / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").unlink()

        destination = root / "installed" / ".agent"
        destination.parent.mkdir(parents=True)
        profiles.copy_brain(source, destination, profile=profiles.STANDARD)

        self.assertTrue((destination / "memory" / "working" / "WORKSPACE.md").exists())
        self.assertTrue(
            (destination / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").exists()
        )

    # --- curated knowledge must survive ----------------------------------

    def test_graduated_candidates_are_preserved(self):
        """Negative control: an over-broad reset would wipe seed lessons."""
        _, destination = self._install()
        graduated = destination / "memory" / "candidates" / "graduated"
        self.assertEqual(
            [p.name for p in graduated.iterdir()], ["2647c12dc81d.json"]
        )
        self.assertIn("vetted", (graduated / "2647c12dc81d.json").read_text())

    def test_semantic_lessons_and_skill_manifest_are_preserved(self):
        _, destination = self._install()
        lessons = destination / "memory" / "semantic" / "lessons.jsonl"
        self.assertIn("curated seed knowledge", lessons.read_text())
        manifest = destination / "skills" / "_manifest.jsonl"
        self.assertIn("shipped", manifest.read_text())

    # --- transient directories -------------------------------------------

    def test_transient_directories_are_not_copied(self):
        _, destination = self._install()
        self.assertFalse((destination / "memory" / "episodic" / "snapshots").exists())
        self.assertFalse((destination / "memory" / "candidates" / "rejected").exists())

    def test_staged_candidates_are_not_copied(self):
        """Unvetted candidates are one repo's pending review, not seed data."""
        _, destination = self._install()
        loose = list((destination / "memory" / "candidates").glob("*.json"))
        self.assertEqual(loose, [])

    # --- per-project runtime state ---------------------------------------

    def test_runtime_state_is_not_copied(self):
        """copytree does not read .gitignore, so these shipped from a clone."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = _source_brain(root / "src")
        (source / ".upgrade-transaction.json").write_text(LEAKED, encoding="utf-8")
        (source / "memory" / "dream-state.json").write_text(LEAKED, encoding="utf-8")
        (source / "memory" / "candidates" / ".lifecycle.lock").write_text(
            LEAKED, encoding="utf-8"
        )
        (source / "runtime").mkdir()
        (source / "runtime" / "pid").write_text(LEAKED, encoding="utf-8")

        destination = root / "installed" / ".agent"
        destination.parent.mkdir(parents=True)
        profiles.copy_brain(source, destination, profile=profiles.STANDARD)

        for relative in (
            ".upgrade-transaction.json",
            "memory/dream-state.json",
            "memory/candidates/.lifecycle.lock",
            "runtime",
        ):
            self.assertFalse((destination / relative).exists(), relative)

    def test_evidence_ledger_is_not_copied(self):
        """An accumulating per-project ledger, empty today but not by design."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = _source_brain(root / "src")
        evidence = source / "memory" / "evidence"
        evidence.mkdir(parents=True)
        (evidence / "README.md").write_text("# Evidence\n", encoding="utf-8")
        (evidence / "revalidation.sqlite3").write_bytes(b"SQLite format 3\x00LEAK")

        destination = root / "installed" / ".agent"
        destination.parent.mkdir(parents=True)
        profiles.copy_brain(source, destination, profile=profiles.STANDARD)

        self.assertFalse((destination / "memory/evidence/revalidation.sqlite3").exists())
        # The documented directory itself still ships.
        self.assertTrue((destination / "memory/evidence/README.md").exists())

    def test_runtime_omissions_match_agent_gitignore(self):
        """Pins the two lists together so .gitignore cannot drift ahead."""
        declared = set()
        for line in (ROOT / ".agent" / ".gitignore").read_text().splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            declared.add(entry.rstrip("/"))
        missing = declared - profiles.runtime_state_paths()
        self.assertEqual(
            missing,
            set(),
            f".agent/.gitignore declares {sorted(missing)} as runtime state, "
            f"but copy_brain would still ship them",
        )

    def test_backup_artifacts_are_not_copied(self):
        """A tracked .bak-20260603 reaches users via the release tarball."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = _source_brain(root / "src")
        refs = source / "skills" / "refs"
        refs.mkdir(parents=True)
        (refs / "drift.json").write_text("{}", encoding="utf-8")
        (refs / "drift.json.bak-20260603").write_text(LEAKED, encoding="utf-8")
        (refs / "notes.md~").write_text(LEAKED, encoding="utf-8")
        (refs / "old.bak").write_text(LEAKED, encoding="utf-8")

        destination = root / "installed" / ".agent"
        destination.parent.mkdir(parents=True)
        profiles.copy_brain(source, destination, profile=profiles.STANDARD)

        kept = sorted(p.name for p in (destination / "skills" / "refs").iterdir())
        self.assertEqual(kept, ["drift.json"])

    # --- profile filtering still works -----------------------------------

    def test_minimal_profile_still_omits_and_still_seeds(self):
        _, destination = self._install(profile=profiles.MINIMAL)
        workspace = destination / "memory" / "working" / "WORKSPACE.md"
        self.assertTrue(workspace.exists())
        self.assertNotIn(LEAKED, workspace.read_text())
        for omitted in profiles.minimal_omitted_paths():
            self.assertFalse((destination / omitted).exists(), omitted)

    # --- end to end against the real repo brain ---------------------------

    def test_real_repo_brain_installs_without_leaking_its_own_state(self):
        """Guards the actual shipped artifact, not just a fixture."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        destination = Path(tmp.name) / ".agent"
        profiles.copy_brain(ROOT / ".agent", destination, profile=profiles.STANDARD)

        workspace = (destination / "memory" / "working" / "WORKSPACE.md").read_text()
        self.assertNotIn("Phase 9", workspace)
        self.assertNotIn("harness_manager", workspace)

        log = destination / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
        self.assertEqual(log.read_text(), "")

        # ...while the curated lessons this repo does intend to ship survive.
        graduated = destination / "memory" / "candidates" / "graduated"
        self.assertTrue(any(graduated.iterdir()))


if __name__ == "__main__":
    unittest.main()
