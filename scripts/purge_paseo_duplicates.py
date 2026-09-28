#!/usr/bin/env python3
"""Remove skill trees Paseo re-creates under the retired `~/.agents/` root.

`~/.agents/` (plural) was retired on 2026-09-21 and must not come back. The
Paseo desktop app wrote skill copies there again on 2026-09-23. This guard runs
every 60s from launchd (`com.syang.agentic-stack.paseo-guard`) and deletes any
directory that reappears.

Only `~/.agents/skills/<name>/` is touched, and only when it is a real
directory. Nothing under the live roots (`~/.agent`, `~/.claude`, `~/.codex`)
is ever read or modified here.
"""
from __future__ import annotations

import json
import shutil
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
    skills = RETIRED_ROOT / "skills"
    if not skills.is_dir():
        return []
    removed = []
    for entry in sorted(skills.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    if removed:
        log(f"purged retired ~/.agents/skills entries: {', '.join(removed)}")
    try:
        skills.rmdir()
        RETIRED_ROOT.rmdir()
        log("purged empty retired ~/.agents root")
    except OSError:
        pass
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
