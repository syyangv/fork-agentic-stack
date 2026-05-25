# agentic-stack v0.19 — "The Agentic Turn"

**Status:** Draft spec
**Author:** codejunkie99
**Target release:** v0.19.0
**Scope:** Convert agentic-stack from a *static portable brain* into an *active multi-agent runtime*.

---

## 0. Goals and non-goals

### Goals
1. Make `.agent/` a runtime, not just a config surface.
2. Let multiple agents coordinate across harnesses on the same project without double-work.
3. Turn accepted lessons into testable, regression-gated behavior contracts.
4. Plan, retrieve, evaluate, and execute speculatively — all local-first.
5. Preserve harness-agnosticism. Nothing here may require a specific LLM, harness, or cloud.

### Non-goals
- Training models. (Flywheel still exports JSONL only.)
- Hosted infra in this release. Cloud is downstream.
- Replacing harnesses' own task loops. We coordinate them; we don't supplant them.

### Design invariants
- **Local-first, file-based.** Every new subsystem is a directory of JSONL + small Python tools. No daemons required for the MVP. File watchers are optional.
- **Append-only audit.** Every state change writes a record. Nothing is destructive except explicit `compact` commands.
- **Permissioned.** Every new tool goes through the existing `protocols/permissions.md` pre-tool-call hook.
- **Cross-platform.** POSIX + Windows. `fcntl` on POSIX, `msvcrt` on Windows, mirror the v0.9 harness manager pattern.

---

## 1. New `.agent/` layout

```
.agent/
├── AGENTS.md
├── plans/                     # NEW — fifth memory layer
│   ├── active/<plan_id>.json
│   ├── archive/<plan_id>.json
│   └── INDEX.md               # human-readable rollup
├── bus/                       # NEW — multi-agent coordination
│   ├── messages.jsonl         # append-only event log
│   ├── claims.jsonl           # who owns what subgoal
│   ├── locks/<resource>.lock  # advisory file locks
│   └── inbox/<agent_id>/      # per-agent unread pointer
├── evals/                     # NEW — behavior contracts
│   ├── cases/<lesson_id>.yaml
│   ├── runs/<timestamp>.jsonl
│   ├── runner.py
│   └── REGRESSIONS.md
├── retriever/                 # NEW — hybrid context
│   ├── index/                 # fastembed + bm25 indices (gitignored)
│   ├── pack.py                # context_pack assembler
│   └── retriever.py
├── spec/                      # NEW — speculative execution
│   ├── worktrees/<spec_id>/   # ephemeral git worktrees
│   ├── runs/<spec_id>.json    # approach, scores, cost
│   └── spec_run.py
├── act/                       # NEW — background autonomous actions
│   ├── policies.yaml          # what auto_act may attempt
│   ├── proposals/<id>.json    # awaiting human review
│   └── auto_act.py
├── memory/                    # EXISTING
├── skills/                    # EXISTING, frontmatter extended
├── protocols/                 # EXISTING, schemas extended
└── tools/                     # EXISTING, plus new CLIs

~/.agentic-stack/global/       # NEW — cross-project federation
├── personal/PREFERENCES.md
├── semantic/lessons.jsonl
├── skills/
└── federation.log
```

---

## 2. Subsystem specs

### 2.1 Plans layer (`.agent/plans/`)

**Purpose.** A fifth memory layer that holds *intent* — what the agent suite is trying to accomplish — separately from working/episodic/semantic/personal memory.

**Plan object (JSON).**
```json
{
  "plan_id": "plan_2026_05_26_a1b2",
  "intent": "Ship v0.19 with Windows hook parity",
  "created_at": "2026-05-26T12:00:00Z",
  "created_by": "human|agent_id",
  "status": "active|blocked|done|abandoned",
  "subgoals": [
    {
      "id": "sg_001",
      "summary": "Port pre_tool_call.py hook to PowerShell",
      "owner": "agent_id|null",
      "depends_on": [],
      "status": "todo|claimed|in_progress|blocked|done",
      "blockers": [],
      "skills_hint": ["git-proxy", "debug-investigator"],
      "evidence": [".agent/episodic/2026-05-26.jsonl#L42"]
    }
  ],
  "context_refs": ["semantic/LESSONS.md#L120-L140"],
  "deadline": null,
  "parent_plan_id": null
}
```

**CLI.**
```
plan.py new      --intent "<text>" [--from-template <name>]
plan.py decompose <plan_id> [--llm <model>]   # uses LLM to expand subgoals
plan.py show     <plan_id>
plan.py next     [--agent <id>]                # returns next unblocked subgoal
plan.py update   <plan_id> --subgoal <sg_id> --status <state>
plan.py block    <plan_id> --subgoal <sg_id> --reason "<text>"
plan.py archive  <plan_id>
```

**Invariants.**
- `next` is deterministic given the same state. No LLM in the path.
- `decompose` is the *only* path that calls an LLM and is always optional.
- Plans may reference but never overwrite memory layers.

**Acceptance.**
- A human runs `plan.py new --intent "X"` then `plan.py decompose`. Output is a tree of subgoals with declared skill hints. Running `plan.py next` twice from two different terminals returns two different subgoals (when bus is wired).

---

### 2.2 Multi-agent bus (`.agent/bus/`)

**Purpose.** Allow N agents (across N harnesses) to coordinate on the same project without stepping on each other.

**Message shape (`messages.jsonl`, append-only).**
```json
{
  "msg_id": "m_2026_05_26_001",
  "ts": "2026-05-26T12:01:03Z",
  "from": "claude-code:host_a",
  "kind": "claim|release|request|result|notice|heartbeat",
  "subject": "plan:plan_2026_05_26_a1b2:sg_001",
  "payload": { "...kind-specific..." },
  "correlates_with": "m_2026_05_26_000",
  "ttl_seconds": 3600
}
```

**Kinds.**
- `claim` — "I'm taking subgoal X for the next T seconds." Writes to `claims.jsonl`.
- `release` — "I'm done or giving up X."
- `request` — "I need skill/result Y from any agent."
- `result` — paired response. Carries a path to artifacts (never inline blobs > 4KB).
- `notice` — non-blocking broadcast (e.g., "test suite green on main").
- `heartbeat` — liveness ping every 60s while a claim is held. Stale claim (> 2 missed heartbeats) is auto-released.

**Claims file (`claims.jsonl`, append-only with periodic compaction).**
```json
{"claim_id":"c_001","subject":"plan:...:sg_001","holder":"claude-code:host_a","granted_at":"...","expires_at":"...","status":"active|released|stolen|expired"}
```

**Locks (`locks/<resource>.lock`).**
Optional advisory mutexes for shared resources (e.g., `package.json`). File contains holder + PID + timestamp; respects file-level OS locking.

**CLI.**
```
bus.py listen  [--agent <id>] [--kind <k>]     # tails messages.jsonl
bus.py post    --kind <k> --subject <s> --payload @file.json
bus.py claim   <subject> [--ttl 1800]
bus.py release <claim_id>
bus.py status                                  # active claims, last 20 msgs
bus.py gc                                      # expire stale claims, compact
```

**Transport.**
- **MVP:** append-only JSONL on disk + filesystem inotify/ReadDirectoryChangesW for `listen`. Works on a single machine, zero deps.
- **v0.20+:** optional NATS or Redis adapter (`bus.py --transport nats://...`). Same message shape, different sink.

**Invariants.**
- The bus is **not** a queue. It is a *journal*. Replayability is the point.
- Every claim has an expiry. No infinite locks.
- `bus.py gc` is safe to run from cron.

**Acceptance.**
- Two terminals run `plan.py next`. Each gets a different subgoal because `claim` prevents double-pick. Killing one terminal causes the claim to expire within 3 heartbeats and the other terminal can pick it up.

---

### 2.3 Eval runner (`.agent/evals/`)

**Purpose.** Turn accepted lessons into regression-tested behavior contracts. A lesson that breaks its own eval is auto-quarantined.

**Case shape (`cases/<lesson_id>.yaml`).**
```yaml
lesson_id: lsn_2026_04_12_payments_idempotency
title: "All payment writes must be idempotent"
contract:
  given: "a function that writes to the payments table"
  must: "include an idempotency_key argument and a SELECT-before-INSERT guard"
  must_not: "perform a naked INSERT without the guard"
fixtures:
  - kind: code_snippet
    path: fixtures/payments_good.py
    expected: pass
  - kind: code_snippet
    path: fixtures/payments_bad.py
    expected: fail
judge:
  kind: rubric_llm | regex | ast_check | shell
  spec: "..."
budget:
  max_tokens: 4000
  max_seconds: 30
```

**Runner.**
```
evals/runner.py run            [--lesson <id>] [--changed-only]
evals/runner.py status
evals/runner.py quarantine <lesson_id> --reason "<text>"
evals/runner.py release    <lesson_id>
```

- Triggered by a git pre-commit hook on `.agent/semantic/lessons.jsonl`.
- A failing eval flips the lesson's `recall_status` to `quarantined`. Quarantined lessons are excluded from `recall.py` until released.
- Run history in `runs/<timestamp>.jsonl`. Regression deltas rendered into `REGRESSIONS.md`.

**Judges.**
- `regex` — simple, deterministic.
- `ast_check` — Python AST or tree-sitter for other langs.
- `shell` — invoke a script with exit code = pass/fail.
- `rubric_llm` — fallback, costs tokens, used sparingly. Rubric prompt is checked in.

**Invariants.**
- Evals never call the network unless `judge.kind == rubric_llm` and a model is configured.
- Fixtures live in `cases/fixtures/` and are git-tracked.
- A new lesson without an eval emits a warning but does not block graduation. After 7 days without an eval, it auto-quarantines.

**Acceptance.**
- `graduate.py` accepts a lesson, runner auto-generates a stub case, human fills in fixtures, `runner run` passes, lesson appears in `recall.py` results.

---

### 2.4 Hybrid retriever (`.agent/retriever/`)

**Purpose.** Replace FTS-only memory search with hybrid BM25 + dense embeddings + reranking, and produce *context packs* sized to a harness's context window.

**Index layout (gitignored).**
```
.agent/retriever/index/
├── bm25/                 # tantivy or simple inverted index
├── embeddings/           # fastembed output, sqlite-vec for storage
└── manifest.json
```

**Components.**
- **Embedder:** `fastembed` with `BAAI/bge-small-en-v1.5` (CPU, ~130MB, ~10ms/chunk). Configurable to swap models.
- **Vector store:** `sqlite-vec` extension (zero-server, file-backed).
- **BM25:** keep existing FTS5 path as a backend option; default to a pure-Python rank-bm25 for cross-platform simplicity.
- **Reranker:** optional cross-encoder via fastembed; off by default to keep cold-start small.

**API.**
```python
from agent.retriever import retrieve, context_pack

results = retrieve(
  query="why does deploy fail on windows runners",
  k=20,
  filters={"layer": ["semantic", "episodic"]},
  rerank=True,
)

pack = context_pack(
  task="implement: port pre_tool_call.py to PowerShell",
  budget_tokens=8000,
  must_include=["semantic/LESSONS.md#L120-L140"],
)
# Returns: {"path": ".agent/working/context_pack_<id>.md", "manifest": [...]}
```

**Pack composition heuristic (default).**
- 30% budget → top-k lessons (semantic).
- 30% → recent episodic entries touching the same files.
- 20% → SKILL.md contents of skills hinted by the task.
- 20% → explicit `must_include` and overflow buffer.

**CLI.**
```
retriever.py index   [--rebuild] [--watch]
retriever.py search  "<query>" [--k 20] [--layer semantic]
retriever.py pack    --task "<text>" --budget 8000 [--out <path>]
retriever.py status
```

**Invariants.**
- All embedding happens locally. Zero network calls.
- Index rebuild is incremental by default (file mtime + content hash).
- A context pack is a **file**, not a stream. Harnesses ingest it via their existing file-include mechanism.

**Acceptance.**
- After `index --rebuild`, `search "deploy failure"` returns the same lesson cluster the FTS path did, plus 2+ semantically related results FTS missed. `pack` emits a markdown file under the token budget verified by `tiktoken`.

---

### 2.5 Skill graph (`skills/` extension)

**Purpose.** Make skills composable. Add typed dependencies and contracts so one skill can call another deterministically.

**Extended SKILL.md frontmatter.**
```yaml
---
name: data-pipeline
version: 0.1.0
triggers: [...existing...]
requires:
  - skill: git-proxy
    version: ">=0.3"
    capabilities: [safe_commit]
  - skill: data-layer
    version: ">=0.2"
provides:
  - capability: pipeline_run
    schema: protocols/tool_schemas/pipeline_run.json
contract:
  inputs:
    pipeline_id: { type: string, required: true }
    window: { type: string, default: "7d" }
  outputs:
    artifact_path: { type: string }
    metrics: { type: object }
side_effects: [writes:.agent/data-layer/exports]
permission_class: medium
---
```

**Resolver (`tools/skill_graph.py`).**
```
skill_graph.py resolve <skill_name>      # prints DAG
skill_graph.py validate                  # checks all manifests for missing deps
skill_graph.py call <skill> --input @in.json --output out.json
```

- `call` is the **typed invocation path**. It looks up `provides`, validates inputs against schema, invokes the skill (either as a Python entry point or as a templated agent instruction), and validates outputs.
- Conflicts (two skills providing the same capability) surface at `validate` time.

**Invariants.**
- Backward compatible: existing skills without `requires`/`provides` keep working as today.
- Cycles in the DAG fail `validate` loudly.
- `permission_class` is enforced by the existing pre-tool-call hook.

**Acceptance.**
- A new `data-pipeline` skill declares deps on `git-proxy` and `data-layer`. `skill_graph.py resolve data-pipeline` prints a 3-node DAG. `skill_graph.py call data-pipeline --input ...` runs end-to-end and produces an artifact.

---

### 2.6 Cross-project federation (`~/.agentic-stack/global/`)

**Purpose.** Personal preferences and reusable lessons live above any single project. Project memory inherits read-only from global; global only accepts writes through explicit promotion.

**Promotion rule.**
A project lesson is eligible for promotion when:
- It has been `accepted` in **3+ distinct projects** (tracked via a content hash + project_id pair).
- Its eval passes in each of those projects.

**CLI.**
```
federate.py status                       # what's global, what's eligible
federate.py promote <lesson_id>          # one-shot, requires confirmation
federate.py demote  <lesson_id>          # remove from global (history retained)
federate.py sync                         # refresh project's view of global
federate.py diff                         # what differs between this project and global
```

**Recall integration.**
`recall.py` queries both `~/.agentic-stack/global/semantic/` and `.agent/semantic/` and tags each result with `[global]` or `[project]`. Project results win on conflict.

**Invariants.**
- Global memory is per-machine, not synced anywhere by default. Users may opt in to git-sync `~/.agentic-stack/global/` themselves.
- No project may write to global except via `federate.py promote`.
- A `permissions.md` rule blocks any agent from writing to `~/.agentic-stack/global/`.

**Acceptance.**
- Accept the same lesson (by content hash) in 3 projects. `federate.py status` lists it as eligible. `federate.py promote` moves it. New project clones see it on first `recall.py` without any setup.

---

### 2.7 Speculative execution (`.agent/spec/`)

**Purpose.** Try N approaches in parallel using git worktrees, score them with the eval runner, present the winner.

**Spec object.**
```json
{
  "spec_id": "spec_2026_05_26_x",
  "task": "fix flaky test in tests/test_upgrade.py",
  "approaches": [
    {"id": "a", "strategy": "retry with backoff", "worktree": ".agent/spec/worktrees/spec_..._a"},
    {"id": "b", "strategy": "deterministic seed", "worktree": "..."},
    {"id": "c", "strategy": "isolate fixture", "worktree": "..."}
  ],
  "scoreboard": {
    "a": {"evals_passed": 4, "evals_failed": 1, "tokens": 12000, "seconds": 90},
    "b": {"evals_passed": 5, "evals_failed": 0, "tokens": 8000,  "seconds": 60},
    "c": {"evals_passed": 3, "evals_failed": 2, "tokens": 15000, "seconds": 110}
  },
  "winner": "b",
  "status": "running|scored|merged|aborted"
}
```

**CLI.**
```
spec_run.py new   --task "<text>" --approaches @strategies.yaml
spec_run.py score <spec_id>
spec_run.py merge <spec_id> --pick b           # fast-forwards winner into a branch
spec_run.py keep-losers <spec_id>              # archives losing diffs as flywheel learning signal
spec_run.py prune <spec_id>
```

**Execution model.**
- MVP: each approach runs sequentially in its own worktree on the local machine.
- v0.20: optional Modal/E2B adapter to fan out in parallel.

**Invariants.**
- Worktrees are temporary; pruned after merge or 7-day TTL.
- Losing approaches are never lost — they're written to `.agent/flywheel/approved-runs.jsonl` with `verdict=lost` so the flywheel learns from them too.
- A spec never auto-merges. Human approval is required.

**Acceptance.**
- `spec_run.py new --task "..." --approaches @three.yaml` creates 3 worktrees. After scoring, `merge --pick b` produces a branch ready for PR. Losing diffs appear in flywheel exports.

---

### 2.8 Background autonomous actions (`.agent/act/`)

**Purpose.** Between sessions, allow a sandboxed agent to take low-risk actions (re-run tests, propose skill rewrites, refresh caches) under tight policy.

**Policy file (`policies.yaml`).**
```yaml
allowed_actions:
  - id: rerun_failed_tests
    trigger: cron:hourly
    permission_class: low
    budget:
      max_tokens: 2000
      max_seconds: 120
      max_runs_per_day: 24
  - id: propose_skill_rewrite
    trigger: on_failure:>=3_in_14d
    permission_class: medium
    requires_human_approval: true
disallowed:
  - any_network_call_outside_allowlist
  - any_write_outside: [.agent/act/proposals, .agent/episodic]
network_allowlist:
  - "api.github.com"
sandbox:
  kind: local|modal|e2b
  spec: { ... }
```

**Proposal shape.**
```json
{
  "proposal_id": "prop_2026_05_26_001",
  "action_id": "propose_skill_rewrite",
  "target": "skills/debug-investigator/SKILL.md",
  "diff_path": ".agent/act/proposals/prop_..._001.patch",
  "rationale": "Failed 4× in 14 days on the 'reproduce' step",
  "supporting_evidence": ["episodic/2026-05-12.jsonl#L88", "..."],
  "status": "pending|approved|rejected|expired",
  "ttl": "7d"
}
```

**CLI.**
```
auto_act.py run-once [--action <id>]            # safe to crontab
auto_act.py list-proposals
auto_act.py approve <proposal_id>
auto_act.py reject  <proposal_id> --reason "<text>"
auto_act.py simulate <action_id>                # dry-run, prints what would happen
```

**Invariants.**
- `auto_act.py` **never** edits source code directly. It only writes patches to `.agent/act/proposals/`.
- Every action emits an episodic record. Every approval/rejection emits a candidate for the dream cycle.
- A proposal that expires (TTL hit without decision) auto-rejects with `reason=expired`.

**Acceptance.**
- `auto_act.py simulate propose_skill_rewrite` prints the patch it would create. `run-once` actually creates it. `approve <id>` applies the patch via `git-proxy`. The whole loop is replayable from `messages.jsonl`.

---

## 3. Protocol additions (`protocols/`)

### 3.1 New permission classes
Extend `permissions.md`:
- `bus_post` — any agent may post messages with `kind in [notice, heartbeat, result]`. `claim`/`release` require an active plan and a valid agent_id.
- `plan_write` — only top-level harness or human may create/archive plans; sub-agents may update subgoal status.
- `act_apply` — only human approval can flip a proposal from `pending` → `approved`.
- `global_write` — only the `federate.py` tool may write to `~/.agentic-stack/global/`.

### 3.2 New tool schemas
Add under `protocols/tool_schemas/`:
- `plan_object.json`
- `bus_message.json`
- `eval_case.json`
- `context_pack_request.json`
- `skill_contract.json`
- `spec_run.json`
- `act_proposal.json`

All schemas are JSON Schema Draft 2020-12, validated by `jsonschema` in CI.

### 3.3 Delegation contract
Extend `delegation.md` to define how a parent agent hands off a subgoal to a sub-agent via the bus:
1. Parent posts `claim` on subgoal.
2. Parent posts `request` referencing the claim with `payload.subgoal_id`.
3. Sub-agent posts `result` referencing the request.
4. Parent verifies outputs against `skill_contract` and posts `release`.

---

## 4. Adapter changes (harness shims)

Each adapter gets one small addition: a way to **announce identity to the bus**.

```bash
# at session start, every adapter calls:
python3 .agent/tools/bus.py announce \
  --agent "claude-code:$(hostname):$$" \
  --capabilities "code_edit,git,tests"
```

- **Claude Code:** wire `bus.py announce` into the existing `PostToolUse` hook setup.
- **Copilot CLI / Gemini / Cursor / Codex:** add a startup instruction to `AGENTS.md` / rules file that tells the agent to call `bus.py announce` on first turn.
- **Standalone Python:** call directly from `run.py`.

No adapter is required to *consume* the bus, but adapters that do (claude-code, copilot-cli, standalone-python) get a `bus.py listen --kind request` companion to pick up delegated work.

---

## 5. CLI surface summary

New top-level verbs on `install.sh` / `install.ps1` / `agentic-stack`:

```
agentic-stack plan      ...   # plans layer
agentic-stack bus       ...   # multi-agent bus
agentic-stack eval      ...   # evals
agentic-stack retrieve  ...   # hybrid retriever
agentic-stack skill     ...   # graph: resolve, validate, call
agentic-stack federate  ...   # cross-project
agentic-stack spec      ...   # speculative execution
agentic-stack act       ...   # background actions
```

All route to the corresponding `.agent/tools/*.py` or top-level subsystem entry points via the existing `harness_manager/cli.py` dispatcher.

---

## 6. Storage, performance, and dependencies

### Footprint
- `.agent/retriever/index/` grows ~5–20 MB per 10k chunks. Gitignored.
- `.agent/spec/worktrees/` can be large (full repo copies). TTL of 7 days, pruned by `spec_run.py gc`.
- `.agent/bus/messages.jsonl` rotates daily; `bus.py gc` compacts after 30 days.

### Dependencies (additive, all optional features fail soft)
- `fastembed` — CPU embeddings.
- `sqlite-vec` — vector storage (single .so/.dll).
- `rank-bm25` — pure-Python BM25 (default; tantivy optional).
- `jsonschema` — schema validation.
- `watchdog` — optional file watcher for `bus.py listen` (falls back to polling).
- `tiktoken` — token budgeting for context packs.

No GPU required. No daemon required. Python 3.10+ (already current minimum after the 3.9 fix in v0.10).

### Performance targets
- `plan.py next`: < 50 ms cold.
- `bus.py post`: < 20 ms.
- `retriever.py search` over 10k chunks: < 200 ms.
- `eval runner` for 50 cases: < 30 s without LLM judges.

---

## 7. Migration and compatibility

### From v0.18 → v0.19
- `agentic-stack upgrade --yes` creates the new directories empty.
- Existing memory, skills, adapters untouched.
- No new files added to git unless the user opts in (`upgrade --enable plans,bus,evals` etc.).
- Feature toggles in `.agent/memory/.features.json`:
  ```json
  {
    "plans.enabled": true,
    "bus.enabled": true,
    "evals.enabled": true,
    "retriever.enabled": false,
    "skill_graph.enabled": false,
    "federation.enabled": false,
    "spec.enabled": false,
    "act.enabled": false
  }
  ```
- Onboarding wizard gets one new step: "Enable agentic runtime features (beta)?"

### Backward compatibility guarantees
- Every existing CLI keeps working unchanged.
- Skills without new frontmatter fields still load.
- A repo where every new feature is disabled behaves identically to v0.18.

---

## 8. Documentation deliverables

New files under `docs/`:
- `docs/agentic-runtime.md` — overview of the eight subsystems and how they compose.
- `docs/plans.md`
- `docs/bus.md`
- `docs/evals.md`
- `docs/retriever.md`
- `docs/skill-graph.md`
- `docs/federation.md`
- `docs/spec.md`
- `docs/act.md`

A new `docs/diagram-runtime.svg` showing:
`intent → plan → bus → [agents in worktrees] → context_pack → skill_graph → eval → dream → federate → act`.

---

## 9. Rollout plan

### Phase 1 — Foundation (1 week)
- Plans layer + bus (MVP, file-only transport).
- New permission classes.
- New schemas.
- `bus.py announce` wired into claude-code and standalone-python adapters.

### Phase 2 — Quality gates (1 week)
- Eval runner + auto-quarantine.
- Pre-commit hook on `lessons.jsonl`.
- Stub auto-generation on `graduate.py`.

### Phase 3 — Context and composition (1 week)
- Hybrid retriever (fastembed + sqlite-vec + bm25).
- Context pack assembler.
- Skill graph resolver + typed `call`.

### Phase 4 — The bold pieces (2 weeks)
- Federation (`~/.agentic-stack/global/`).
- Speculative execution with worktrees.
- Background actions with proposals workflow.

### Phase 5 — Polish and launch (1 week)
- All eight `docs/*.md` files.
- Updated `agentic-stack dashboard` to surface plans, claims, proposals.
- Mission Control gains a "Runtime" tab.
- Launch post: *"agentic-stack v0.19 — The Agentic Turn."*

Total: ~6 weeks of focused work.

---

## 10. Risks and open questions

| Risk | Mitigation |
|---|---|
| File-bus contention on Windows | Use `msvcrt.locking` like harness_manager already does; fall back to polling listen. |
| Eval judges drift (LLM rubrics non-deterministic) | Prefer regex/ast/shell judges. LLM judge requires `temperature=0` + fixed seed + golden output snapshot. |
| Auto-quarantine causes lesson churn | Quarantine threshold = 2 consecutive failures on different commits, not 1. |
| Federation leaks secrets across projects | Reuse the existing `transfer_bundle.py` secret-scanner before any promotion write. |
| Worktrees fill disk | Hard TTL + `spec_run.py gc` in nightly cron. |
| Bus replay confuses agents on restart | `bus.py announce` always emits a fresh `agent_id`; replay only matters for forensics, not state. |

**Open questions.**
1. Should `plan.py decompose` use the host harness's LLM (whichever is invoking) or a project-pinned default model? Leaning host's LLM with an override flag.
2. Do we need a `subscriptions.jsonl` so agents can declare interest in specific subjects, or is `bus.py listen --kind X` filter enough? MVP says the latter.
3. Should the retriever index include `.agent/flywheel/` exports? Probably yes, gated behind a config flag.

---

## 11. Definition of done for v0.19

- All eight subsystems present, each behind a feature flag, each defaulting to **off** except plans + bus + evals.
- `agentic-stack upgrade --yes` from v0.18 leaves a working project that opts into the three default-on subsystems.
- `agentic-stack dashboard` shows live plan, claims, and proposal counts.
- Two design partner projects run a full loop: human posts intent → plan decomposes → two harnesses claim subgoals via bus → context packs assembled → evals pass → lesson federated → auto_act proposes a skill rewrite → human approves.
- Launch post published. Top-of-HN attempt.

— end of spec —
