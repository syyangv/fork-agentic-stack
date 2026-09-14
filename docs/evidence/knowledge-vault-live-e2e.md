# Agent Knowledge Vault — Synthetic Live E2E Evidence

Date: 2026-09-14
Worktree: `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault`

## Scope and boundary

This run used the installed production Python knowledge-core CLI boundary:

```text
/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/.agent/tools/knowledge.py
```

Authorized data locations were used only for synthetic work-vault and runtime
state:

```text
work vault: /Users/syang/obsidian/agent-knowledge
runtime:    /Users/syang/.agent/knowledge
temporary:  /private/tmp/knowledge-vault-e2e-20260914
```

No real provider task, personal source, credential, network, commit, push, live
config change, implementation/test/plan/tracker edit, or personal-vault read
was used. The only worktree file written by this run is this evidence file.

The installed plugin UI/RPC path was not callable. The exact attempts were:

```text
mcp__paseo__browser_new_tab({})
=> The browser did not respond before the timeout. Try again or check the browser host.

/Users/syang/.local/bin/paseo plugin ls --json
=> {"error":{"code":"UNKNOWN_ERROR","message":"Transport closed (code 1006)"}}
```

The `paseo` command also emitted an Electron codesign warning. No UI/RPC
result below is presented as live plugin RPC evidence; the results are the
installed production Python/core boundary. Live plugin UI/RPC acceptance is
**OPEN**.

## Five bounded synthetic task cases

### Task A — bounded search and context: **PASS**

Identity: `task_e2e_a_20260914`; the core context CLI has no `turn_id`
parameter, so the turn identity is not applicable at this boundary.

Query: `SYNTHETIC_E2E_SOURCE_MARKER_ALPHA`

Results:

- Search: `status=ok`, `count=1`.
- Context: `status=ok`, `success=true`, `char_count=354`,
  `truncated=false`, `personal_context_used=false`, `stale_sources=[]`.
- Actual source: `7e9a30a0f08f005bc034876b13d9547c436e9f907e8a70bafb77cdc1e3f87477`,
  `10-Projects/synthetic-e2e-source-20260914.md`, content hash
  `1824e61fa59314dc2b322af05b44f294a6e434325811ffe2a47e9fe01aacacf0`.

### Task B — draft, explicit approval, formal write, affected-path refresh: **PASS**

Identity: `task_e2e_b_20260914`, `turn_e2e_b_20260914`.

Capture result:

- `capture_status=draft_created`, `success=true`.
- Draft: `draft_aec7fc2190e67366b7714cbd7014d99381f7f62f28dcceb74e6846fd059f049f`.
- Candidate hash:
  `be9abb986716cb4434c3a9a13d62d47a0a88d8979d85bc1b4755bc9111325533`.
- Draft hash:
  `5aae2a3f659c4178cffad2f8d7347f81e95452f11661b4d766db6d104900538a`.
- Draft target: `00-Inbox/synthetic-e2e-approved-candidate-20260914.md`.

Human approval and apply result:

- Approver: `synthetic-human-reviewer-20260914`.
- Formal target: `20-Research/synthetic-e2e-approved-20260914.md`.
- `status=accepted`, `target_action=create`, `idempotent=false`.
- `desired_hash` and `post_write_hash` both equal the draft hash above.
- `index_updated=true` (affected formal path refresh).
- Runtime audit: one draft row, one committed accept review; target path is
  formal and not Inbox.

### Task C — separate-task approved reuse: **PASS**

Identity: `task_e2e_c_20260914`; the core context CLI has no `turn_id`
parameter, so the turn identity is not applicable at this boundary.

Query: `SYNTHETIC_E2E_APPROVED_MARKER_BETA`.

Results:

- Search: `status=ok`, `count=2`; both hits are two indexed locations in the
  same formal note, `20-Research/synthetic-e2e-approved-20260914.md`.
- Context: `status=ok`, `success=true`, `char_count=796`,
  `truncated=false`, `personal_context_used=false`, `stale_sources=[]`.
- No `00-Inbox` result was returned. This is a separate task ID from Task B,
  so the result demonstrates cross-task reuse from the approved formal note,
  not task-local draft reuse.

### Task D — duplicate capture idempotency: **PASS**

Identity: `task_e2e_d_20260914`, `turn_e2e_d_20260914`.

Results:

- First capture: `draft_created`, draft
  `draft_d5539d148755fe97d86137c5097bf75f31b39a118d7370ee6513552b8c07400e`.
- Second identical capture: `duplicate`, same draft ID and same draft hash
  `aad6bd8bd324086c9bc4a497c0face53b0e0d7f96dcdf976cbed8e17defa4175`.
- A measured third identical capture also returned `duplicate`.
- Runtime has one draft row for this task/turn and one Inbox artifact:
  `00-Inbox/synthetic-e2e-idempotent-20260914.md`.
- Inbox artifact SHA-256 remained
  `aad6bd8bd324086c9bc4a497c0face53b0e0d7f96dcdf976cbed8e17defa4175`;
  `mtime_before_ns=1789400287718021233` and
  `mtime_after_ns=1789400287718021233` on the measured duplicate.

### Task E — stale context and failed-turn capture: **PASS**

Identity: `task_e2e_e_20260914`, `turn_e2e_e_20260914`.

The indexed synthetic stale fixture was mutated without refreshing its derived
index. The old query then produced:

- Context: `status=stale_sources`, `success=false`, `context=""`,
  `char_count=0`, `source_manifest=[]`, `personal_context_used=false`.
- The stale source was explicitly reported with its old content hash; no stale
  text was sent in the context bundle.
- Failed-turn capture: `capture_status=capture_incomplete`, `success=false`,
  diagnostic `turn_not_completed`.
- Runtime has zero draft rows and no
  `00-Inbox/synthetic-e2e-stale-rejected-20260914.md` artifact for this task.

This is a stale-plus-failed-turn proof, not a real provider timeout proof.
Provider timeout/reconciliation through live RPC remains **OPEN**.

## Performance plan measurements

These measurements are separate from the existing 20-query recall/location
evidence in `docs/evidence/knowledge-vault-p7-synthetic-evaluation.md`. That
file remains the source of truth for recall@5 and source-location correctness;
these measurements do not recompute or conflate those metrics.

Corpus and runtime:

- Indexed work-vault corpus: **5 notes**, **21 chunks**; Inbox drafts are not
  part of the searchable formal corpus.
- Platform/runtime: `macOS-14.5-arm64-i386-64bit`, Python `3.12.7`, SQLite
  `3.45.3`, executable `/Users/syang/miniconda3/bin/python3`.
- Query set (20): `SYNTHETIC_E2E_SOURCE_MARKER_ALPHA`, `bounded`, `context`,
  `retrieval`, `explicit`, `human`, `approval`, `formal`, `path`, `cross-task`,
  `reuse`, `affected`, `refresh`, `approved`, `synthetic`, `acceptance`,
  `source`, `work-vault`, `idempotent`, `stale`.
- Boundary: wall-clock duration around a complete installed `knowledge.py`
  subprocess, including Python startup, CLI JSON parsing, SQLite open/query,
  and stdout capture. Warm samples followed a full rebuild and one untimed
  warm-up; filesystem/SQLite cache was warm but each sample used a fresh
  process.
- p95: sorted samples, nearest-rank index `ceil(0.95*n)-1`; `n=20` for the
  two target metrics.

Results:

| Measurement | Samples | p95 | Target | Result |
|---|---:|---:|---:|---|
| Warm search | 20 | 53.682 ms | <= 1 s | **PASS** |
| Warm context-prepare | 20 | 58.654 ms | <= 3 s | **PASS** |
| Full index rebuild | 1 | 57.473 ms | reported separately | recorded |

Separate path-scoped refresh samples were three idempotent replays of the
accepted Task B decision. They returned `status=accepted`, `idempotent=true`,
and the same formal target; the index was already current, so
`index_updated=false` on each no-op replay. Durations were 45.553 ms,
45.708 ms, and 48.018 ms (nearest-rank p95 48.018 ms). The original Task B
apply above is the mutation sample and returned `index_updated=true`.

## Exact commands and final checks

Representative acceptance commands (all paths and IDs are literal):

```sh
/Users/syang/miniconda3/bin/python3 .agent/tools/knowledge.py index \
  --work-vault /Users/syang/obsidian/agent-knowledge \
  --runtime-dir /Users/syang/.agent/knowledge

/Users/syang/miniconda3/bin/python3 .agent/tools/knowledge.py search \
  --query SYNTHETIC_E2E_SOURCE_MARKER_ALPHA \
  --task-id task_e2e_a_20260914 \
  --work-vault /Users/syang/obsidian/agent-knowledge \
  --runtime-dir /Users/syang/.agent/knowledge --limit 5

/Users/syang/miniconda3/bin/python3 .agent/tools/knowledge.py build_context \
  --task-id task_e2e_a_20260914 \
  --query SYNTHETIC_E2E_SOURCE_MARKER_ALPHA \
  --work-vault /Users/syang/obsidian/agent-knowledge \
  --runtime-dir /Users/syang/.agent/knowledge --limit 5 --max-snippets 5 --max-chars 4000

/Users/syang/miniconda3/bin/python3 .agent/tools/knowledge.py propose_note \
  --task-id task_e2e_b_20260914 --turn-id turn_e2e_b_20260914 \
  --task-status completed --draft-store /Users/syang/.agent/knowledge/drafts.sqlite3 \
  --allow-protected-runtime --draft-vault /Users/syang/obsidian/agent-knowledge \
  < /private/tmp/knowledge-vault-e2e-20260914/task-b-envelope.json

/Users/syang/miniconda3/bin/python3 .agent/tools/knowledge.py review_apply \
  --work-vault /Users/syang/obsidian/agent-knowledge \
  --draft-store /Users/syang/.agent/knowledge/drafts.sqlite3 \
  --journal-path /Users/syang/.agent/knowledge/apply-journal.jsonl \
  --db-path /Users/syang/.agent/knowledge/index.sqlite3 \
  --draft-id draft_aec7fc2190e67366b7714cbd7014d99381f7f62f28dcceb74e6846fd059f049f \
  --draft-hash 5aae2a3f659c4178cffad2f8d7347f81e95452f11661b4d766db6d104900538a \
  --target-path 20-Research/synthetic-e2e-approved-20260914.md \
  --approved-by synthetic-human-reviewer-20260914 --human-approved \
  < /private/tmp/knowledge-vault-e2e-20260914/task-b-approval.json
```

Final relevant regression checks:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/syang/miniconda3/bin/python3 -m pytest -q \
  -p no:cacheprovider tests/test_knowledge_*.py
# 66 passed in 1.37s

(cd integrations/paseo-knowledge && npm run typecheck)
# exit 0

./integrations/paseo-knowledge/node_modules/.bin/vitest run \
  --config integrations/paseo-knowledge/vitest.config.mjs \
  --configLoader runner --reporter=dot
# 1 test file passed; 21 tests passed
```

The full repository-wide regression suite was not rerun for this evidence-only
acceptance pass. No implementation or test changes were made.

## Acceptance summary

| Gate | Status |
|---|---|
| A–E installed Python/core synthetic cases | **PASS** |
| Warm search and context targets | **PASS** |
| Existing 20-query recall/location evidence preserved separately | **PASS** |
| Direct installed plugin UI/RPC execution | **OPEN** — browser host timeout / transport closed |
| Real provider task or provider timeout/reconciliation | **OPEN** |
| Ordinary-path disable/reenable compatibility | **OPEN** — no live config change was authorized |
