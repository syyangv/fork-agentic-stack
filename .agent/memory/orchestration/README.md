# Federated memory orchestration

The orchestrator preserves three authority-ordered lanes:

1. **governance** — accepted agentic-stack permissions, preferences, decisions,
   and lessons;
2. **behavioral** — project-scoped MemOS hypotheses and experience;
3. **evidence** — current revision-bound CRG and executed-test provenance.

## Assist rollout gate

Source support for `assist` does not activate it automatically. The checked-in
configuration remains `off` until a local `assist-quality.json` proves every
documented gate: 50 completed episodes, five task categories, duplicate rate
below 5%, at least 30 evaluated queries with precision@5 of 0.70 or better,
zero cross-project leaks, and p95 recall below 750 ms. Missing or incomplete
metrics fail closed to governance-only recall while event capture continues in
shadow mode. `memory_orchestrate.py health` exposes the configured and effective
modes plus every failed gate.

The default metrics path is
`.agent/runtime/memos/<project-id>/assist-quality.json`; tests and evaluators may
override it with `AGENTIC_ASSIST_METRICS`.

## Retrieval and lifecycle

`memory_orchestrate.py recall --run-id <id> --reason <boundary>` emits a bounded
context packet and JSON retrieval preview. Fusion ranks inside each lane, keeps
governance first, suppresses stale or duplicate items, and records only the
items actually selected for injection—not every raw provider hit.

Supported lifecycle reasons are `task_start`, `decision_point`, `recovery`,
`user_feedback`, and `completion`. The event journal records first-failure
recovery attempts and selected item outcomes (`used`, `contradicted`, or
`ignored`). Task completion sends those outcomes and bounded verification
evidence to MemOS as context hints.

MemOS search is namespace-scoped and has a 700 ms total local deadline. Thin
2.0.10 search hits are enriched through read-only detail methods. Skill detail
uses `skill.list`; `skill.get` is intentionally avoided because it records a
usage/trial side effect. Missing ownership, hub-shared results, foreign project
ownership, sensitive plaintext, and malformed results are never injected.
