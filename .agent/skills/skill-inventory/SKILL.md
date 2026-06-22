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

# Skill Inventory

Track skill availability across Claude Code, Codex, and `.agent` roots.

## Default audit

```bash
python3 ~/.agentic-stack/.agent/tools/skill_inventory.py
```

This reports:
- counts per root
- one-root-only skills (`claude-only`, `codex-only`, `agent-only`)
- per-skill `classification` (`agent-only`, `codex-only`, `claude-only`, `all-roots`, or pairwise shared)
- Claude/Codex pairwise drift
- shared resolved targets, including symlink mirrors and aliases

## Machine-readable audit

```bash
python3 ~/.agentic-stack/.agent/tools/skill_inventory.py --format json
```

Use JSON for automation, dashboards, or CI checks.

## Drift gate

```bash
python3 ~/.agentic-stack/.agent/tools/skill_inventory.py --fail-on-drift
```

This exits non-zero if Claude and Codex differ. Use it before claiming skill sync is complete.

## Roots

Defaults:
- Claude: `~/.claude/skills`
- Codex: `~/.codex/skills`
- Agent/shared: `~/.agent/skills`

Override roots with:

```bash
python3 ~/.agentic-stack/.agent/tools/skill_inventory.py \
  --claude-root /path/to/claude/skills \
  --codex-root /path/to/codex/skills \
  --agents-root /path/to/agents/skills
```

## Policy

- Treat Codex-only infrastructure skills (`doctor`, `help`, `worker`, `omx-setup`, `codex-hooks-manager`) as expected unless the user asks for strict parity.
- Treat user/domain skills that are only in one harness as migration candidates.
- Prefer moving the source of truth into Claude Code when the user wants Claude and Codex parity, then run `bash ~/.agentic-stack/install.sh add codex` to sync Codex.


## Reviewed drift registry

Known intentional one-root-only differences are recorded in:

```text
~/.agentic-stack/.agent/skills/skill-inventory/references/reviewed-drift.json
```

The inventory report marks these as `reviewed`; any other `*-only` skill is `unchecked` and should be inspected before assuming sync is healthy.

Strict gate for unchecked one-root-only drift:

```bash
python3 ~/.agentic-stack/.agent/tools/skill_inventory.py --fail-on-unchecked-drift
```
## Secondary skill registry policy

`~/.claude/skills` and `~/.agent/skills` are secondary registries for Codex.
Do not host those skills under `~/.codex/skills` just to make them visible.
Codex should consult `~/.claude/skills/<name>/SKILL.md` and
`~/.agent/skills/<name>/SKILL.md` at their original paths when the
injected/native skill list lacks a requested skill or capability.
`~/.codex/skills` is reserved for Codex-local skills.

### Dynamic runtime visibility test

After instruction changes, run both CLIs and fail if either CLI exits with zero
model turns or does not acknowledge the secondary registry policy:

```bash
python3 ~/.agentic-stack/.agent/tools/verify_skill_registry_visibility.py
```

Use `--skip-claude` or `--skip-codex` only when that CLI is intentionally unavailable.
A Claude JSON result with `num_turns: 0`, empty `result`, or `duration_api_ms: 0`
is not a pass; it means the CLI process launched but the model test did not execute.

