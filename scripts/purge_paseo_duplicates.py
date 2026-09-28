#!/usr/bin/env python3
"""Remove skill trees Paseo re-creates under the retired `~/.agents/` root.

`~/.agents/` (plural) was retired on 2026-09-21 and must not come back. The
Paseo desktop app wrote skill copies there again on 2026-09-23. This guard runs
every 60s from launchd (`com.syang.agentic-stack.paseo-guard`) and deletes any
directory that reappears.

The whole `~/.agents/` tree is retired, so every entry under
`~/.agents/skills/` is removed regardless of whether it looks like a skill —
Paseo has been observed creating both complete trees and bare directories.
Nothing under the live roots (`~/.agent`, `~/.claude`, `~/.codex`) is ever
read or modified here.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

RETIRED_ROOT = Path.home() / ".agents"
LOG_PATH = Path.home() / "Library" / "Logs" / "agentic-stack-paseo-guard.log"
INTERVAL = 60


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def purge() -> list[str]:
    """Remove everything under the retired root, symlinks included but never followed."""
    if not RETIRED_ROOT.exists() and not RETIRED_ROOT.is_symlink():
        return []
    if RETIRED_ROOT.is_symlink():
        # A symlink at the root is not a tree we created; drop just the link.
        RETIRED_ROOT.unlink()
        log("purged symlink at retired ~/.agents path")
        return ["."]
    removed = []
    # Deepest first so parents are empty by the time we try them; a plain sort
    # would try `skills` before `skills/<name>` and leave the tree behind.
    for entry in sorted(RETIRED_ROOT.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
            else:
                entry.unlink()
        except OSError:
            continue
        removed.append(str(entry.relative_to(RETIRED_ROOT)))
    if removed:
        log(f"purged {len(removed)} retired ~/.agents entries: {', '.join(removed[:20])}")
    try:
        RETIRED_ROOT.rmdir()
        log("purged retired ~/.agents root")
    except OSError:
        log("retired ~/.agents root not empty after purge; left in place")
    return removed


def main() -> int:
    # One-shot by default so launchd's StartInterval can drive it; --watch keeps
    # a resident loop for callers that prefer that.
    if "--watch" in sys.argv:
        while True:
            purge()
            time.sleep(INTERVAL)
    print(json.dumps({"purged": purge(), "retired_root_exists": RETIRED_ROOT.exists()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
