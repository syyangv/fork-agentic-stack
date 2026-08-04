#!/usr/bin/env python3
"""Two-way mirror top-level context files between $HOME and the agentic-stack repo.

Pairs:
  ~/AGENTS.md  <->  ~/.agentic-stack/AGENTS.md
  ~/CLAUDE.md  <->  ~/.agentic-stack/CLAUDE.md

Rule: when contents differ, the newer mtime wins. A repo checkout / pull
refreshes the repo file's mtime (repo wins); a home edit refreshes the home
file's mtime (home wins). Equal mtimes with differing content is a conflict
(logged, left untouched). Runs are skipped while git holds the repo lock.

Override the two roots with SYNC_HOME / SYNC_REPO env vars (for testing).
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME_ROOT = Path(os.environ.get("SYNC_HOME", str(Path.home())))
REPO_ROOT = Path(os.environ.get("SYNC_REPO", str(Path.home() / ".agentic-stack")))
NAMES = ("AGENTS.md", "CLAUDE.md")
LOG_PATH = Path(os.environ.get("SYNC_LOG", str(Path.home() / "Library" / "Logs" / "agentic-stack-context-sync" / "context-sync.log")))


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {msg}\n")
    except OSError as exc:
        print(f"context-sync: cannot write log: {exc}", file=sys.stderr)


def read_or_none(path: Path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def copy_if_different(src: Path, dst: Path) -> bool:
    src_bytes = read_or_none(src)
    if src_bytes is None:
        return False
    if read_or_none(dst) == src_bytes:
        return False
    tmp = dst.with_name(f"{dst.name}.tmp")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True
    except OSError as exc:
        log(f"ERROR copying {src} -> {dst}: {exc}")
        return False


def sync_pair(name: str) -> None:
    home, repo = HOME_ROOT / name, REPO_ROOT / name
    home_bytes, repo_bytes = read_or_none(home), read_or_none(repo)
    if home_bytes is None and repo_bytes is None:
        return
    if home_bytes is None:
        if copy_if_different(repo, home):
            log(f"created {home} from {repo}")
        return
    if repo_bytes is None:
        if copy_if_different(home, repo):
            log(f"created {repo} from {home}")
        return
    if home_bytes == repo_bytes:
        return
    home_mt = home.stat().st_mtime_ns
    repo_mt = repo.stat().st_mtime_ns
    if home_mt > repo_mt:
        if copy_if_different(home, repo):
            log(f"home newer -> {home} -> {repo}")
    elif repo_mt > home_mt:
        if copy_if_different(repo, home):
            log(f"repo newer -> {repo} -> {home}")
    else:
        log(f"CONFLICT equal mtime, differing content: {name} untouched")


def main() -> int:
    if (REPO_ROOT / ".git" / "index.lock").exists():
        log("skip: git operation in progress (index.lock present)")
        return 0
    if not REPO_ROOT.exists():
        log("skip: repo root missing")
        return 1
    for name in NAMES:
        try:
            sync_pair(name)
        except OSError as exc:
            log(f"ERROR {name}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
