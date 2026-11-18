---
name: skill-inventory
version: 2026-05-17
description: Audit skill availability across Claude Code, Codex, and .agent roots. Use when checking whether skills are Claude-only, Codex-only, missing after sync, duplicated by symlink aliases, or when maintaining cross-harness skill parity.
triggers: ["skill inventory", "skill drift", "codex-only", "claude-only", "missing skills", "sync skills", "skill parity"]
tools: [bash, python3]
preconditions: []
constraints: ["audit before deleting or moving skills", "do not remove Codex-only infrastructure skills unless explicitly requested"]
category: meta
---

# Skill Inventory (stub)

This file is a pointer. The full rules live in one place:

**`/Users/syang/.agent/skills/skill-inventory/SKILL.md`** (`~/.agent/skills/skill-inventory/SKILL.md`)

Read that file for the audit commands, drift gates, roots, policy, reviewed-drift
registry, tag-writing safety, and secondary-registry policy.

## What stays in this directory

`references/` (`provenance.json`, `reviewed-drift.json`) stays here.
`~/.agentic-stack/.agent/tools/skill_inventory.py` resolves it from its own
`__file__`, and `~/Library/Scripts/skill_inventory_weekly.py` hardcodes this
path. Keep it in place.

## Why a stub

This tree is the installer's seed source, and `_manifest.jsonl` rows are rendered
from this file's frontmatter, so the frontmatter above stays unchanged. The body
had drifted from the canonical copy (Requests #84a, #85); one canonical body
removes the second copy that drifted. The previous body is in git history.
