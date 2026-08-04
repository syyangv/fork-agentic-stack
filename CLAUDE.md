# Project Instructions (Claude Code)

> **Single source of truth: `AGENTS.md`** (repo root, mirrored to `~/AGENTS.md`).
> It is the merged context for pi, Codex, and Claude Code. **Read
> `AGENTS.md` first and follow it.** This file only carries what is specific
> to Claude Code.

## Claude-Code-specific

- **PostToolUse hook**: every tool call is captured automatically, but the
  reflections are mechanical. For significant events, call
  `python3 .agent/tools/memory_reflect.py` explicitly with a rich `--note`
  (guide + examples live in `AGENTS.md` → Manual memory logging).
- Read order matches `AGENTS.md`: `.agent/AGENTS.md` → PREFERENCES →
  REVIEW_QUEUE → LESSONS → permissions.

Everything else — recall-first, skills (`~/.agent/skills/`, paseo only
there), workspace discipline, git remotes, and all override rules — is
defined once, in `AGENTS.md`.
