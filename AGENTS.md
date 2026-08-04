# AGENTS.md — Project instructions (agentic-stack portable brain)

Merged default context for pi and Codex: this file combines the instructions
previously split across `AGENTS.md` and `CLAUDE.md`. Pi loads this file at
startup (only one context file per directory, `AGENTS.md` wins), so everything
from both files lives here. `CLAUDE.md` remains for Claude Code.

> **Python invocation**: examples below use `python3`. On stock Windows
> only `python` is on PATH; use whichever resolves on your system.

## Session start — read in this order
1. `.agent/AGENTS.md` — the map of the whole brain
2. `.agent/memory/personal/PREFERENCES.md` — how the user works
3. `.agent/memory/working/REVIEW_QUEUE.md` — pending lessons awaiting review
4. `.agent/memory/semantic/LESSONS.md` — what we've already learned
5. `.agent/protocols/permissions.md` — hard constraints, read before any tool call

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
All skills live in the portable brain at `~/.agent/skills/` — the one
source of truth. The `~/.agents/` mirror is retired and skills are not
duplicated to `~/.claude/skills/` or `~/.codex/skills/` (no symlinks, no
copies). Read `.agent/skills/_index.md` and load a full `SKILL.md` only
when its triggers match the task (progressive disclosure). If another
harness needs the skills, point it at `~/.agent/skills` (e.g. pi's
`settings.json` `"skills"` array) instead of copying them out.

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
