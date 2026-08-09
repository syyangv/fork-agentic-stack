# AGENTS.md — Project instructions (agentic-stack portable brain)

Merged default context for pi and Codex: this file combines the instructions
previously split across `AGENTS.md` and `CLAUDE.md`. Pi loads this file at
startup (only one context file per directory, `AGENTS.md` wins), so everything
from both files lives here. `CLAUDE.md` remains for Claude Code.

> **Python invocation**: examples below use `python3`. On stock Windows
> only `python` is on PATH; use whichever resolves on your system.

## Session start — read in this order
1. `.agent/protocols/permissions.md` — hard constraints; load before other tool-driven startup reads
2. `.agent/AGENTS.md` — the map of the whole brain
3. `.agent/memory/personal/PREFERENCES.md` — how the user works
4. `.agent/memory/working/WORKSPACE.md` — current task state
5. `.agent/memory/working/REVIEW_QUEUE.md` — pending lessons awaiting review
6. `.agent/memory/semantic/DECISIONS.md` — past architectural choices
7. `.agent/memory/semantic/LESSONS.md` — what we've already learned
8. `.agent/memory/episodic/AGENT_LEARNINGS.jsonl` — raw experience log (top-k by salience)

## Before every non-trivial action — recall first

For any task involving **deploy**, **ship**, **release**, **migration**,
**schema change**, **supabase**, **edge function**, **timestamp** /
**timezone** / **date**, **failing test**, **debug**, **investigate**, or
**refactor**, run recall FIRST and present the results before acting:

```bash
python3 .agent/tools/recall.py "<one-line description of what you're about to do>"
```

Show the output in a `Consulted lessons before acting:` block. If a surfaced
lesson would be violated by your intended action, stop and explain why.

## Skills
Skills may live in any of the three cross-harness registries:
`~/.agent/skills/`, `~/.claude/skills/`, and `~/.codex/skills/`. Check all
three before declaring a skill unavailable, load a match from its original
path, and do not create symlink or copy mirrors merely to make it visible to
another harness. The canonical registry policy lives in `.agent/AGENTS.md`.

Portable-brain and Paseo-owned skills remain canonical in `~/.agent/skills/`;
host/plugin-owned skills may remain in their native Claude or Codex registry.
The retired `~/.agents/` mirror must not be restored. Read
`.agent/skills/_index.md` for portable-brain discovery and load a full
`SKILL.md` only when its triggers match the task (progressive disclosure).
`scripts/purge_paseo_duplicates.py` (launchd
`com.syang.agentic-stack.paseo-guard`, 60s) removes duplicate Paseo copies
that the desktop app may reinstall.

## While working

### Workspace
Update `.agent/memory/working/WORKSPACE.md` when:
- You start a new task (write the goal and first step)
- Your hypothesis changes
- You complete or abandon a task (clear it so the next session is clean)

### Brain state
Quick overview any time:
```bash
python3 .agent/tools/show.py
```

### Teaching the agent a new rule
When you discover something that should never happen again:
```bash
python3 .agent/tools/learn.py "<the rule, phrased as a principle>" \
    --rationale "<why — include the incident that taught you this>"
```

## Manual memory logging — when and how

The PostToolUse hook captures every tool call automatically, but its
reflections are mechanical. For **significant events** you must call
`memory_reflect.py` explicitly with a rich `--note`. These are the entries
the dream cycle promotes into lessons.

### When to log manually
- After completing a major feature or fixing a bug that took real investigation
- After any rollback, incident, or unexpected failure
- After any architectural decision (why you chose approach A over B)
- After discovering a project-specific constraint (e.g. "this table has a
  trigger that fires on every insert — don't bulk insert")
- After a Supabase migration, RLS policy change, or edge function deploy
- Any time you think "I wish I had known this an hour ago"

### How to write a good entry

```bash
# Good: specific, domain-rich, future-oriented
python3 .agent/tools/memory_reflect.py \
    "supabase-migration" \
    "applied add_user_tier_column migration" \
    "migration succeeded; 847 rows backfilled to tier=free" \
    --importance 8 \
    --note "RLS policy on user_profiles must be updated whenever a new column is added that affects row visibility. Missed this, caused 401s in staging for 20 minutes."

# Good: failure with root cause
python3 .agent/tools/memory_reflect.py \
    "edge-function" \
    "deployed notify-on-signup" \
    "deploy failed: missing RESEND_API_KEY in production env" \
    --fail \
    --importance 9 \
    --note "Production env vars for edge functions must be set in supabase secrets, not .env. The .env file is ignored at deploy time."

# Bad: vague, no content words for clustering
python3 .agent/tools/memory_reflect.py \
    "claude-code" "did stuff" "ok" --importance 3
```

### Importance guide
| Value | When |
|---|---|
| 9–10 | Production incident, data migration, rollback, security issue |
| 7–8 | Deploy, schema change, architectural decision, non-obvious constraint |
| 5–6 | Refactor, significant bug fix, API contract change |
| 3–4 | Routine edit, file creation, test run |

## Git remotes (personal fork)
- `origin` = `git@github.com:syyangv/fork-agentic-stack.git` (SSH, writable)
- `upstream` = `https://github.com/codejunkie99/agentic-stack.git` (HTTPS, read-only)
- Rebase workflow: `git fetch upstream && git rebase upstream/master`
- `adapters/gemini/` is locally enhanced (hooks, auto-memory, merge-safe install) — diff before accepting upstream Gemini adapter updates

## Rules that override all defaults
- Never force push to `main`, `production`, or `staging`.
- Never delete episodic or semantic memory entries — archive them.
- Never modify `.agent/protocols/permissions.md` — only humans edit it.
- Never hand-edit `.agent/memory/semantic/LESSONS.md` — use `graduate.py`.
- If `REVIEW_QUEUE.md` shows pending > 10 or oldest > 7 days, review
  candidates before starting substantive work.
- To delete a file use `/bin/rm` by absolute path — bare `rm` is aliased to
  the uninstalled `trash`, so it fails with `command not found: trash` and
  silently deletes nothing.
- Paseo skills live only in `~/.agent/skills/` — do not duplicate to
  `~/.claude/skills/` or `~/.codex/skills/`.

## Session Learnings (Paseo)
- **Paseo daemon PATH:** The launchd daemon (`com.syang.paseo.daemon`) has a restricted PATH: `~/.local/bin:~/projects/codex-hooks/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`. It does NOT include `~/.npm-global/bin`. Any npm-global-installed provider binary (like `pi`) needs a wrapper script in `~/.local/bin/<name>` that execs `node@22` on the real entrypoint. A daemon restart (`paseo restart`) is required after adding the wrapper for provider re-discovery.
