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

## Promotion and revalidation

Phase 7 translates only bridge-observable, exactly project-owned MemOS policy,
world-model, and skill DTOs into `agentic.memory.candidate.v1` review records.
Upstream `active` remains a behavioral observation and always maps to local
`staged`; only an explicit human reviewer command can write `accepted`.
`memory_orchestrate.py candidates --intent ...` previews candidates, and
`--stage` adds them to the review queue.

The pinned MemOS 2.0.10 bridge exposes no policy-list or decision-repair
list/get RPC. Policies may therefore be discovered only through search and
safe enrichment. Repair-shaped policy guidance remains a policy candidate;
the system never opens MemOS SQLite or pretends a trace is a decision repair.
`skill.get` remains forbidden because namespaced calls record use/trial state.

Code-specific graduation requires current project/revision-bound CRG evidence
and a separate passing executed-test ledger row. A structural `TESTED_BY`
relationship is association only, never proof that a test ran. Revalidation
appends `revalidation_needed` rather than deleting guidance, immediately
removes the latest lesson state from recall, and installs a local stale
override for linked MemOS records without calling upstream archive/delete.
Every behavioral candidate must receive an explicit human scope decision at
its first graduation: `--non-code-confirmed` or one or more
`--code-ref FILE::QUALIFIED_SYMBOL` arguments. Upstream code flags are hints,
not authority, and a graduated classification cannot be changed during final
acceptance. Code classification also requires nonempty CRG-covered references.
Reviewers attach replacement revision-bound graph and test rows with repeated
`--evidence-ref evi_<64-lowercase-hex>` arguments before initial graduation or
reacceptance; the live gate validates those exact rows before any semantic
state is appended.

Human review is available through `memory_review.py inspect|provisional|accept|
reject|defer|retract|reopen`. Inspection reconstructs provider IDs, evidence
rows, verification metadata, and lesson transitions. Human rejection and
deferral remain terminal until an explicit reopen. An explicitly accepted
`revalidation_needed` candidate is live-validated again, appends a new accepted
lesson state, and only then clears its local MemOS stale override.

## Opus-only full-evolution pilot safety gate

Phase 8 infrastructure is opt-in through an owner-only, exact-schema
`AGENTIC_EVOLUTION_PILOT_CONFIG` bound to one canonical repository root and
project ID. The default profile remains `lightweightMemory.enabled: true`.
Recognized managed profiles can be switched back to lightweight mode without
deleting behavioral data.

The `agentic.memory.evolution-pilot.v2` contract permits only provider
`claude_opus` and an Opus routing label. The native reverse boundary accepts
only exact, post-normalization MemOS 2.0.10 L2/L3/skill system contracts and
two-message role shapes. Per-operation allowlist translators reconstruct
metadata-only DTOs (opaque IDs/digests, bounded taxonomy tags/tool names,
numeric evidence, and outcome metadata); raw user/agent text, policy bodies,
reflections, source, prompts, commands, paths, and tool input/output never
cross. Unknown prompt families—including summarization and session classifiers—
fail closed. Capture reflection scoring/synthesis, reward scoring, feedback
repair, L3 trace bodies, and retrieval filtering are disabled in the pilot
profile; their local deterministic/heuristic paths remain available. Host calls use stdin rather than argv,
an allowlisted environment containing only process basics, non-secret macOS
user/keychain routing variables, plus the existing
`CLAUDE_CODE_OAUTH_TOKEN`, an empty owner-only working directory, Claude's
preventive no-tools mode, bounded process groups, owner-only transactional daily
quotas, digest-idempotent caching, and metadata-only audit records.

Opus output remains non-authoritative: it cannot accept a candidate. The
20-task paired held-out gate binds both arms to one project/revision and one
canonical task-definition plus harness protocol, resolves canonical CRG
test-run evidence, and requires the assisted execution to reference the exact
orchestrator-recorded context and skill-injection digests. It requires no
success-rate regression and at least 10% median cost/recovery improvement among
successful pairs before the pilot can be accepted. GPT/Codex hosting remains
blocked: `CodexGPTAdapter` fails closed with `codex_no_tools_unavailable` until
Codex offers a genuinely preventive no-tools execution surface.

The immutable MemOS code prefix carries an installer-generated inventory of
every regular file and internal symlink. Activation recomputes the complete
path/type/size/SHA-256 set and rejects modified or extra executable content.

## Behavioral backup and rollback

MemOS 2.0.10 has no backup RPC. Every compliant provider session holds the
stable project lifecycle lock for its lifetime. The lock is a sibling of the
project root rather than a child, so its inode survives an atomic project-tree
swap and already-waiting providers cannot bypass restore exclusion.
`memos_backup.create_project_backup`
first acquires that same lock (and therefore cannot overlap a live compliant
bridge session), validates the managed profile and SQLite health, then
snapshots the entire project root
(journal, profile/config, MemOS data including SQLite WAL/SHM state, skills,
logs, and daemon state) into an owner-only directory with a SHA-256 manifest.
The bridge must be cooperatively closed before backup or restore. Restore
verifies every digest, stages a complete tree, validates its managed config and
all runtime SQLite databases with read-only `quick_check`, atomically swaps it
into place, and preserves the replaced tree as an owner-only rollback
directory. It never copies only a live `memos.db` and never deletes the prior
state automatically. Callers must still run bridge `core.health` before
resuming event delivery; the local restore helper cannot attest a process that
has intentionally not yet been restarted.
