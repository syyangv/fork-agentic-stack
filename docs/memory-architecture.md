# Memory Architecture

This guide explains how local agentic-stack memory, external Brain, and
Headroom fit together. They solve different problems and are not
interchangeable stores.

## The Three Planes

```text
                         current model request
                                  |
                         Headroom (optional)
                    runtime context compression
                                  |
                 +----------------+----------------+
                 |                                 |
          agentic-stack                       external Brain
             .agent/                              ~/.brain
      operational memory                  selected long-term notes
```

### agentic-stack: operational memory

The `.agent/` tree is the source of truth for how an agent works in a project.
It contains:

- `memory/working/` for current task state and handoffs
- `memory/episodic/` for captured events and session traces
- `memory/semantic/` for reviewed lessons and project decisions
- `memory/personal/` for user preferences
- `skills/` and `protocols/` for executable workflow guidance

The global `~/.agent/` tree provides user-wide defaults. A project-local
`.agent/` tree owns repository-specific rules when one is installed. Do not
silently merge the two stores.

The local lifecycle is:

```text
tool hooks or reflection
        -> episodic history
        -> auto_dream stages recurring patterns
        -> human reviews candidates
        -> graduate.py writes semantic lessons
        -> connected harnesses read the result
```

Candidate generation is not approval. A recurring pattern must pass the
`agentic-stack-review` human review path before it becomes a semantic lesson.

### Brain: selected external memory

The external Brain CLI and MCP server use `~/.brain` as a separate Git-backed
event store. Brain is appropriate for concise observations that should survive
across projects, harnesses, or machines.

Brain does not scan repositories, import `.agent` memory, or replace local
lessons. Promotion is explicit:

```text
local classification
        -> Brain ask for duplicates/context
        -> explicit Brain note
        -> optional brain push to the configured remote
```

The `codejunkie99/brain` repository is the Brain implementation. A user's
private repository, such as `owner/brain`, stores that user's event history.
Do not use the public implementation repository as a memory remote.

### Headroom: runtime context management

Headroom is a context-management layer, not a memory store. It can compress or
route the current model request to reduce token usage when context is large.
It does not:

- write lessons to `.agent`
- write notes to `~/.brain`
- replace `recall.py`, `auto_dream.py`, or Brain search
- make Brain synchronization automatic

If an agent includes a local lesson or Brain result in a model prompt,
Headroom may compress that prompt according to its runtime policy. The data
still belongs to its original memory plane.

## Ownership Rules

| Question | Owner |
|---|---|
| How should this harness behave? | `.agent/skills` and `.agent/protocols` |
| What is the current project constraint? | Project-local `.agent/memory/semantic` |
| What user preference applies everywhere? | Global `.agent/memory/personal` |
| What should another machine or non-agentic-stack harness remember? | External Brain |
| How should an oversized request be compressed? | Headroom |
| Should a candidate become a permanent local lesson? | Human review via `agentic-stack-review` |

Keep one authoritative copy of each rule. A related Brain note is justified only
when the distilled observation has an independent cross-project use. Do not
mirror full `LESSONS.md`, raw episodic logs, or project notes into Brain.

## Automatic Behavior

After setup, the following can happen without starting a background Brain
daemon:

- Configured agent hooks capture local activity into agentic-stack episodic
  memory according to the harness adapter.
- The configured scheduler can run `auto_dream.py` to stage local candidates.
- Claude Code, Codex, and OpenCode can start `brain serve --mcp` on demand when
  their MCP configuration enables Brain.
- Brain updates its local derived index when a note is written or the CLI opens
  the store.
- Headroom applies runtime context policy when its proxy is in the request path.

These actions are independent. A local hook event does not automatically become
a Brain event, and a Brain event does not automatically become a local lesson.

## Manual Operations

Check local agentic-stack memory:

```bash
python3 .agent/tools/show.py
python3 .agent/tools/recall.py "topic or intended action"
```

Review and consolidate local memory:

```bash
python3 .agent/tools/list_candidates.py
python3 .agent/tools/graduate.py <id> --rationale "why this is reusable"
python3 .agent/tools/reject.py <id> --reason "why this is not durable"
```

Use external Brain explicitly:

```bash
python3 .agent/tools/brain_bridge.py status
python3 .agent/tools/brain_bridge.py ask "topic or decision"
python3 .agent/tools/brain_bridge.py note "one concise durable observation"
brain doctor
brain push
brain pull
```

`brain note` commits the event to the local Brain repository. `brain push` and
`brain pull` are explicit remote operations; there is no implicit cloud sync.

## Promotion Checklist

Before writing a Brain note, confirm:

1. The observation is stable beyond the current task.
2. It applies across projects, harnesses, or machines.
3. It can be stated concisely without repo-specific implementation detail.
4. It contains no credentials, secrets, or unnecessary private data.
5. `brain ask` did not find an existing equivalent note.

If any answer is no, keep the information in `.agent` or in the project
documentation instead.

## Existing Repositories and Sessions

Installing or enabling external Brain does not retroactively import existing
repositories or session learnings. Existing `.agent/` files remain available to
agentic-stack, and existing project documentation remains the authority for
that project.

To promote an older item safely, extract only the durable principle, ask Brain
for duplicates, save the distilled note, and push when remote backup is wanted.
Do not bulk-copy historical logs.

## Troubleshooting

### Brain MCP is unavailable

Check the CLI and local store first:

```bash
python3 .agent/tools/brain_bridge.py status
brain doctor --deep
```

Then inspect the harness-specific MCP configuration. The expected server
command is:

```text
brain serve --mcp
```

### A local lesson is missing

Use `recall.py` against the relevant `.agent` tree and inspect the candidate
queue. Do not assume that a Brain note is a substitute for a reviewed local
lesson.

### Brain and local memory disagree

Apply the ownership table above. Project facts belong to project-local
`.agent`; user-wide operational policy belongs to global `.agent`; Brain holds
only explicitly promoted cross-project observations. Resolve the source first,
then update or archive the stale representation.

### Context is too large

Treat this as a Headroom/runtime issue first. Inspect compression and context
threshold behavior; do not respond by copying more raw history into Brain.
