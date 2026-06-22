# Project Instructions (Gemini CLI)

This project uses the **agentic-stack** portable brain. Shared memory,
skills, and protocols live in `.agent/`.

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

## While working

### Skills
Gemini scans `.gemini/skills/` at startup. The installer mirrors
`.agent/skills` into `.gemini/skills` using a merge, so shared skills are
added while existing Gemini-local skills are preserved. Load the matching
`SKILL.md` for any task whose triggers apply.

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

## Automatic memory

Project-level hooks in `.gemini/settings.json` automatically:
- Log significant `run_shell_command`, `replace`, `write_file`, and
  `write_todos` results into `.agent/memory/episodic/AGENT_LEARNINGS.jsonl`
- Run `python3 .agent/memory/auto_dream.py` when the Gemini session ends

Manual `memory_reflect.py` calls are still required for major decisions,
incidents, migrations, and other events where the plain tool payload is not
enough context.

## Rules that override all defaults
- Never force push to `main`, `production`, or `staging`.
- Never delete episodic or semantic memory entries — archive them.
- Never modify `.agent/protocols/permissions.md` — only humans edit it.
- Never hand-edit `.agent/memory/semantic/LESSONS.md` — use `graduate.py`.
- If `REVIEW_QUEUE.md` shows pending > 10 or oldest > 7 days, review
  candidates before starting substantive work.

## Surface boundaries

- Direct Gemini CLI sessions use this `GEMINI.md` + `.gemini/settings.json`
  integration surface.
- Claude-side workflows that invoke Gemini through wrappers such as
  `codeagent-wrapper --backend gemini` are a separate orchestration surface.
  They may call Gemini, but they do not replace this workspace-level Gemini
  CLI integration.
