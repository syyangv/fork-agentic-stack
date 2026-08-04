#!/usr/bin/env python3
"""Guard: keep paseo skills out of the harness skill dirs.

The Paseo desktop app hardcodes three skill install targets
(~/.agents/skills, ~/.claude/skills, ~/.codex/skills) and reinstalls its
bundled skills into any missing target on every GUI launch — there is no
config to redirect it. Rule (AGENTS.md): paseo lives only in
~/.agent/skills. This guard enforces that rule: it removes paseo-named
skill dirs from the three hardcoded targets and prunes now-empty parent
dirs, so duplicates never survive longer than one launchd interval.

Only directories whose name starts with "paseo" are ever touched.
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
GUARD_DIRS = (
    HOME / ".agents" / "skills",
    HOME / ".claude" / "skills",
    HOME / ".codex" / "skills",
)
LOG_PATH = HOME / "Library" / "Logs" / "agentic-stack-paseo-guard" / "paseo-guard.log"


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {msg}\n")
    except OSError as exc:
        print(f"paseo-guard: cannot write log: {exc}", file=sys.stderr)


def prune_empty_parents(start: Path, stop_at: Path) -> None:
    """Remove empty dirs from `start` upward, stopping before `stop_at`."""
    cur = start
    while cur != stop_at and cur != cur.parent:
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def main() -> int:
    removed_any = False
    for skills_dir in GUARD_DIRS:
        if not skills_dir.is_dir():
            continue
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            if not entry.name.startswith("paseo"):
                continue
            shutil.rmtree(entry, ignore_errors=True)
            log(f"removed {entry}")
            removed_any = True
        # Prune empty parents (e.g. ~/.agents/skills and ~/.agents) up to HOME.
        if removed_any:
            prune_empty_parents(skills_dir, HOME)
            removed_any = False
    return 0


if __name__ == "__main__":
    sys.exit(main())
