# Architecture

Five modules, one principle: the harness is dumb, and the knowledge,
telemetry, and reusable artifacts are in local files.

## Modules

### Memory — four layers
- **working/** — live task state. Volatile. Archived after 2 days.
- **episodic/** — what happened in prior runs. JSONL, scored by salience.
- **semantic/** — distilled patterns that outlive episodes.
- **personal/** — user-specific preferences. Never merged into semantic.

### Federated memory orchestration — three providers

- **Governance (agentic-stack)** remains authoritative for permissions,
  preferences, decisions, and human-approved lessons.
- **Behavioral (MemOS local 2.0.10)** records project-isolated episodes and
  traces through a digest-pinned artifact and committed npm dependency lock,
  with telemetry disabled. Phase 3 is
  shadow-only: its health is visible but its retrieval results are never
  inserted into prompts.
- **Evidence (CRG)** remains the structural code graph. Its lifecycle is
  independent of governance maintenance and MemOS task capture.

The MemOS code tree is immutable and separate from mutable per-project data.
Each project gets a private `MEMOS_HOME`, synthetic `HOME`, SQLite delivery
journal, and upstream episode-ID mapping. The bridge subprocess receives a
sanitized environment, uses bounded JSON-RPC, and retries only calls known to
be idempotent. Tool capture sends bounded summaries through `turn.end`; raw
stdout, environment data, and model reasoning do not cross the provider
boundary. External events can be submitted singly or as a bounded batch with
`memory_orchestrate.py record`; batching preserves one bridge lifecycle for a
complete task. Upstream-compatible `turn.start`, `turn.end`, `memory.search`,
and `feedback.submit` calls receive a 75-second deadline; health, session, and
other light calls retain the smaller bounded bridge default, while episode and
session finalization receive 15 seconds to flush capture state. Initial bridge
health receives a 75-second cold-start allowance. One total
deadline is shared across retries, pipe writes, and response waits, so a
backpressured child cannot hang the recorder. A project-scoped OS lock keeps
one bridge lifecycle active per project, keeps claim-through-terminal delivery
FIFO across concurrent recorders, and makes crash recovery unambiguous without
reclaiming a live request. Phase 4 invokes the shadow recorder through a
single redacting hook boundary. Each adapter sends only events exposed by its
native hook API. Small private correlation records connect the behavioral
event/run IDs to the existing episodic entry. Hooks atomically enqueue into a
private spool; a detached, serialized worker delivers bounded batches and
writes structured health. Provider startup and degradation therefore remain
off the hook critical path and cannot block Stop/SessionEnd. `export-shadow`
remains redacted and byte-bounded.

The exact native capability matrix and explicit commands for hookless
harnesses live in `docs/harness-event-capabilities.md`.

The two daily governance LaunchAgents do not own MemOS or CRG lifecycle.
Provider health/retry/retention jobs may be introduced only behind separate,
tested provider-owned contracts. Phase 9 converts the governance jobs into
installer-managed thin launchers without adding behavioral or graph work.

### Skills — progressive disclosure
- `_index.md` and `_manifest.jsonl` always in context (tiny).
- A full `SKILL.md` loads only when its triggers match the current task.
- Every skill has a self-rewrite hook at the bottom.

### Protocols — contracts with external systems
- `permissions.md` — allow / approval-required / never-allowed.
- `tool_schemas/` — typed interfaces for every external tool.
- `delegation.md` — rules for sub-agent handoff.

### Data layer — local visibility across harnesses
- `.agent/tools/data_layer_export.py` normalizes shared episodic memory,
  optional harness events, and optional cron runs.
- `.agent/data-layer/` is private runtime state and exports.
- Exports include JSONL, CSV, KPI summaries, `dashboard.html`, and
  `daily-report.md`.
- The dashboard helps users see harness mix, cron schedules, active agents,
  token/cost estimates, categories, and workflow outcomes across Claude Code,
  Hermes, OpenClaw, Codex, Cursor, OpenCode, and other adapters.

### Data flywheel — approved work becomes reusable artifacts
- `.agent/tools/data_flywheel_export.py` reads sanitized approved runs.
- `.agent/flywheel/` is private runtime state and exports.
- Exports include redacted trace records, context cards, eval cases,
  training-ready JSONL, and flywheel metrics.
- The flywheel prepares retrieval, evals, prompt shrinking, and optional future
  open-weight adapter work. It does not train models.

## The feedback loops

1. Skills log to episodic memory after every action.
2. Memory-manager detects recurring patterns and promotes them to semantic.
3. Skillforge watches for patterns not yet covered by existing skills.
4. Failures fire `on_failure.py`, which flags skills for rewrite after 3+
   hits in 14 days.
5. Constraint violations inside a skill escalate from local `KNOWLEDGE.md`
   to global `LESSONS.md`.
6. Data-layer exports turn local activity into screenshot-ready monitoring
   without adding remote telemetry.
7. Human-approved runs can be exported into flywheel artifacts when the user
   wants a private corpus for recurring workflows.

## Why the separation matters

You can swap the harness for any of the adapters (Claude Code,
Cursor, Windsurf, OpenCode, OpenClaw, Copilot CLI, Gemini, Hermes, Pi, Codex,
standalone Python, Antigravity) and lose nothing. The brain is portable; only
the glue changes. The dashboard and flywheel work for the same reason: every
harness can write to the same local `.agent/` event stream.

See `diagram.svg` for a visual.
