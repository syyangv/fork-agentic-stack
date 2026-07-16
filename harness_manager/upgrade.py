"""Project-local .agent infrastructure upgrade."""
from __future__ import annotations

import fnmatch
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

from . import skill_manifest


def upgrade(
    target_root: Path | str,
    stack_root: Path | str,
    *,
    dry_run: bool = False,
    yes: bool = False,
    log: Callable[[str], None] | None = None,
) -> int:
    """Copy safe skeleton-owned .agent files into an installed project."""
    if log is None:
        log = print
    target_root = Path(target_root)
    stack_root = Path(stack_root)
    src_agent = stack_root / ".agent"
    dst_agent = target_root / ".agent"
    if not dst_agent.is_dir():
        print(f"error: {dst_agent} not found; install agentic-stack first", file=sys.stderr)
        return 2

    actions = _plan(src_agent, dst_agent)
    if not actions:
        log(f"{target_root}: .agent infrastructure already current")
    else:
        log(f"{'would update' if dry_run else 'updating'} {len(actions)} .agent file(s):")
        for src, dst in actions:
            log(f"  {'~' if dst.exists() else '+'} {dst.relative_to(target_root)}")

    if dry_run:
        log("dry run; no files changed")
        return 0

    if not yes and sys.stdin.isatty():
        answer = input("apply upgrade? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            log("aborted; no files changed")
            return 0
    if not yes and not sys.stdin.isatty():
        print("error: upgrade needs confirmation; re-run with --yes or --dry-run", file=sys.stderr)
        return 2

    for src, dst in actions:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    _merge_agent_gitignore(src_agent, dst_agent, log=log)
    skill_manifest.sync_manifest(target_root, log=log)
    return 0


def _merge_agent_gitignore(
    src_agent: Path, dst_agent: Path, *, log: Callable[[str], None]
) -> bool:
    """Upsert stack runtime ignores without replacing user-owned rules."""
    src = src_agent / ".gitignore"
    if not src.is_file():
        return False
    source_text = src.read_text(encoding="utf-8")
    required = [
        line.strip()
        for line in source_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    dst = dst_agent / ".gitignore"
    existing = dst.read_text(encoding="utf-8") if dst.is_file() else ""
    existing_lines = set(existing.splitlines())
    missing = [line for line in required if line not in existing_lines]
    if not missing:
        return False
    if existing:
        separator = "" if existing.endswith("\n") else "\n"
        addition = (
            separator
            + "\n# agentic-stack runtime coordination and health state\n"
            + "\n".join(missing)
            + "\n"
        )
        merged = existing + addition
    else:
        merged = source_text
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gitignore-", suffix=".tmp", dir=dst.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(merged)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    log(f"  ~ {dst.relative_to(dst_agent.parent)} (merged runtime ignores)")
    return True


def _plan(src_agent: Path, dst_agent: Path) -> list[tuple[Path, Path]]:
    actions: list[tuple[Path, Path]] = []
    for rel in _infrastructure_files(src_agent):
        src = src_agent / rel
        dst = dst_agent / rel
        if _needs_copy(src, dst):
            actions.append((src, dst))

    src_index = src_agent / "skills" / "_index.md"
    dst_index = dst_agent / "skills" / "_index.md"
    if src_index.is_file() and _needs_copy(src_index, dst_index):
        actions.append((src_index, dst_index))

    src_skills = src_agent / "skills"
    dst_skills = dst_agent / "skills"
    for skill_md in sorted(src_skills.glob("*/SKILL.md")):
        skill_dir = skill_md.parent
        if (dst_skills / skill_dir.name).exists():
            continue
        for src in sorted(p for p in skill_dir.rglob("*") if p.is_file() and not _ignored(p)):
            rel = src.relative_to(src_agent)
            actions.append((src, dst_agent / rel))
    return actions


def _infrastructure_files(src_agent: Path) -> list[Path]:
    rels: list[Path] = []
    manifest = src_agent / "infrastructure.json"
    if manifest.is_file():
        rels.append(manifest.relative_to(src_agent))
    for base in ("harness",):
        root = src_agent / base
        if root.is_dir():
            rels.extend(p.relative_to(src_agent) for p in root.rglob("*.py") if not _ignored(p))
    for base in ("memory", "tools"):
        root = src_agent / base
        if root.is_dir():
            rels.extend(p.relative_to(src_agent) for p in root.glob("*.py") if not _ignored(p))
    return sorted(rels)


def _ignored(path: Path) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts:
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in ("*.pyc", "*.pyo"))


def _needs_copy(src: Path, dst: Path) -> bool:
    if not dst.is_file():
        return True
    try:
        return src.read_bytes() != dst.read_bytes()
    except OSError:
        return True
