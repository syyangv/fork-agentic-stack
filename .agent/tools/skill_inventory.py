#!/usr/bin/env python3
"""Inventory Claude/Codex/agentic-stack skills and report drift.

Compares skill directories across harness roots and highlights skills that are
available in only one harness. The audit is intentionally local-only and does
not mutate skill folders.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_ROOTS = {
    "claude": Path("~/.claude/skills"),
    "codex": Path("~/.codex/skills"),
    "agent": Path("~/.agent/skills"),
}

DEFAULT_REVIEWED_DRIFT = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "skill-inventory"
    / "references"
    / "reviewed-drift.json"
)


@dataclass
class SkillEntry:
    key: str
    frontmatter_name: str
    path: str
    resolved: str
    symlink: bool
    description: str = ""


def _read_frontmatter(skill_md: Path) -> tuple[str, str]:
    name = skill_md.parent.name
    description = ""
    try:
        lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return name, description

    in_frontmatter = bool(lines and lines[0].strip() == "---")
    if not in_frontmatter:
        return name, description

    for line in lines[1:80]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"').strip("'") or name
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip('"').strip("'")
    return name, description


def scan_root(root: Path, alias_prefixes: Iterable[str] = ("00-",)) -> dict[str, SkillEntry]:
    root = root.expanduser()
    entries: dict[str, SkillEntry] = {}
    if not root.exists():
        return entries

    for skill_md in sorted(root.glob("*/SKILL.md")):
        directory = skill_md.parent
        dir_name = directory.name
        fm_name, description = _read_frontmatter(skill_md)

        # A priority alias such as 00-pua points to the same underlying skill.
        # Index by frontmatter name so aliases do not create false drift.
        key = fm_name if any(dir_name.startswith(prefix) for prefix in alias_prefixes) else dir_name
        entries[key] = SkillEntry(
            key=key,
            frontmatter_name=fm_name,
            path=str(skill_md),
            resolved=str(directory.resolve()),
            symlink=directory.is_symlink(),
            description=description,
        )
    return entries


def load_reviewed_drift(path: Path | None) -> dict[str, object]:
    if path is None:
        path = DEFAULT_REVIEWED_DRIFT
    path = path.expanduser()
    if not path.exists():
        return {
            "reviewed_codex_only": {},
            "reviewed_claude_only": {},
            "reviewed_agent_only": {},
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "reviewed_codex_only": {},
            "reviewed_claude_only": {},
            "reviewed_agent_only": {},
        }
    for key in ("reviewed_codex_only", "reviewed_claude_only", "reviewed_agent_only"):
        data.setdefault(key, {})
    return data


def review_key_for_classification(classification: str) -> str | None:
    return {
        "codex-only": "reviewed_codex_only",
        "claude-only": "reviewed_claude_only",
        "agent-only": "reviewed_agent_only",
    }.get(classification)

def classify_presence(present: list[str]) -> str:
    roots = set(present)
    if roots == {"agent"}:
        return "agent-only"
    if roots == {"claude"}:
        return "claude-only"
    if roots == {"codex"}:
        return "codex-only"
    if roots == {"claude", "codex"}:
        return "claude-codex"
    if roots == {"claude", "agent"}:
        return "claude-agents"
    if roots == {"codex", "agent"}:
        return "codex-agents"
    if roots == {"claude", "codex", "agent"}:
        return "all-roots"
    return "unknown"


def build_inventory(roots: dict[str, Path], reviewed_drift: dict[str, object] | None = None) -> dict[str, object]:
    if reviewed_drift is None:
        reviewed_drift = load_reviewed_drift(DEFAULT_REVIEWED_DRIFT)
    scanned = {name: scan_root(path) for name, path in roots.items()}
    all_names = sorted(set().union(*(set(items) for items in scanned.values())))

    matrix = []
    for skill in all_names:
        present = sorted(name for name, items in scanned.items() if skill in items)
        missing = sorted(name for name in scanned if name not in present)
        classification = classify_presence(present)
        review_key = review_key_for_classification(classification)
        review_note = ""
        reviewed = False
        if review_key:
            reviewed_map = reviewed_drift.get(review_key, {}) or {}
            if skill in reviewed_map:
                reviewed = True
                review_note = str(reviewed_map[skill])
        matrix.append({
            "skill": skill,
            "classification": classification,
            "reviewed": reviewed,
            "review_note": review_note,
            "present": present,
            "missing": missing,
        })

    only = {
        name: sorted(skill for skill in all_names if [r for r in scanned if skill in scanned[r]] == [name])
        for name in scanned
    }

    pairwise = {
        "codex_not_claude": sorted(set(scanned["codex"]) - set(scanned["claude"])),
        "claude_not_codex": sorted(set(scanned["claude"]) - set(scanned["codex"])),
        "agent_not_claude": sorted(set(scanned["agent"]) - set(scanned["claude"])),
        "claude_not_agent": sorted(set(scanned["claude"]) - set(scanned["agent"])),
        "agent_not_codex": sorted(set(scanned["agent"]) - set(scanned["codex"])),
        "codex_not_agent": sorted(set(scanned["codex"]) - set(scanned["agent"])),
    }

    duplicates_by_resolved: dict[str, list[dict[str, str]]] = {}
    for root_name, items in scanned.items():
        for skill, entry in items.items():
            duplicates_by_resolved.setdefault(entry.resolved, []).append({
                "root": root_name,
                "skill": skill,
                "path": entry.path,
            })
    aliases = {
        resolved: refs
        for resolved, refs in sorted(duplicates_by_resolved.items())
        if len(refs) > 1
    }

    return {
        "roots": {name: str(path.expanduser()) for name, path in roots.items()},
        "reviewed_drift": reviewed_drift,
        "counts": {name: len(items) for name, items in scanned.items()},
        "only": only,
        "pairwise": pairwise,
        "matrix": matrix,
        "aliases": aliases,
        "entries": {
            root_name: {skill: asdict(entry) for skill, entry in items.items()}
            for root_name, items in scanned.items()
        },
    }


def _print_text(report: dict[str, object]) -> None:
    counts = report["counts"]
    print("# Skill Inventory Drift Report")
    print()
    print("## Counts")
    for root in ("claude", "codex", "agent"):
        print(f"- {root}: {counts[root]}")

    print()
    print("## One-root-only skills")
    only = report["only"]
    for root in ("claude", "codex", "agent"):
        skills = only[root]
        print(f"- {root}-only ({len(skills)}): {', '.join(skills) if skills else 'none'}")

    print()
    print("## Reviewed one-root-only drift")
    matrix = report["matrix"]
    for classification in ("codex-only", "claude-only", "agent-only"):
        rows = [row for row in matrix if row["classification"] == classification]
        reviewed = [row for row in rows if row.get("reviewed")]
        unchecked = [row for row in rows if not row.get("reviewed")]
        print(f"- {classification}: reviewed {len(reviewed)} / unchecked {len(unchecked)}")
        if unchecked:
            print(f"  unchecked: {', '.join(row['skill'] for row in unchecked)}")

    print()
    print("## Classification counts")
    matrix = report["matrix"]
    class_counts: dict[str, int] = {}
    for row in matrix:
        class_counts[row["classification"]] = class_counts.get(row["classification"], 0) + 1
    for name in sorted(class_counts):
        print(f"- {name}: {class_counts[name]}")

    print()
    print("## Skill classification matrix")
    for row in matrix:
        present = "/".join(row["present"])
        checked = " reviewed" if row.get("reviewed") else " unchecked" if row["classification"].endswith("-only") else ""
        print(f"- {row['skill']}: {row['classification']}{checked} ({present})")

    print()
    print("## Claude/Codex sync drift")
    pairwise = report["pairwise"]
    print(f"- codex_not_claude ({len(pairwise['codex_not_claude'])}): {', '.join(pairwise['codex_not_claude']) if pairwise['codex_not_claude'] else 'none'}")
    print(f"- claude_not_codex ({len(pairwise['claude_not_codex'])}): {', '.join(pairwise['claude_not_codex']) if pairwise['claude_not_codex'] else 'none'}")

    aliases = report["aliases"]
    print()
    print(f"## Shared resolved skill targets ({len(aliases)})")
    for resolved, refs in list(aliases.items())[:20]:
        label = ", ".join(f"{ref['root']}:{ref['skill']}" for ref in refs)
        print(f"- {label} -> {resolved}")
    if len(aliases) > 20:
        print(f"- ... {len(aliases) - 20} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit skill drift across Claude, Codex, and .agent roots.")
    parser.add_argument("--claude-root", type=Path, default=Path(os.environ.get("CLAUDE_SKILLS_ROOT", DEFAULT_ROOTS["claude"])))
    parser.add_argument("--codex-root", type=Path, default=Path(os.environ.get("CODEX_SKILLS_ROOT", DEFAULT_ROOTS["codex"])))
    parser.add_argument("--agent-root", type=Path, default=Path(os.environ.get("AGENT_SKILLS_ROOT", DEFAULT_ROOTS["agent"])))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--reviewed-drift", type=Path, default=DEFAULT_REVIEWED_DRIFT, help="JSON registry of reviewed one-root-only skill drift.")
    parser.add_argument("--fail-on-drift", action="store_true", help="Exit 1 when Claude/Codex pairwise drift exists.")
    parser.add_argument("--fail-on-unchecked-drift", action="store_true", help="Exit 1 when any one-root-only skill is not reviewed.")
    args = parser.parse_args(argv)

    reviewed_drift = load_reviewed_drift(args.reviewed_drift)
    report = build_inventory({
        "claude": args.claude_root,
        "codex": args.codex_root,
        "agent": args.agent_root,
    }, reviewed_drift=reviewed_drift)

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)

    pairwise = report["pairwise"]
    if args.fail_on_drift and (pairwise["codex_not_claude"] or pairwise["claude_not_codex"]):
        return 1
    if args.fail_on_unchecked_drift:
        unchecked = [
            row for row in report["matrix"]
            if row["classification"].endswith("-only") and not row.get("reviewed")
        ]
        if unchecked:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
