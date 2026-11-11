# Knowledge Vault — Replacement Coordinator Recovery Checkpoint

Created: 2026-09-20
Purpose: stable recovery handoff before any future daemon restart request.
Safety rule: **do not restart, merge, reload, toggle, or update installed source from this checkpoint until this file is read back and the coordinator confirms preservation.**

## Original objective

Implement and accept the governed dual-vault Agent Knowledge loop:

- synthetic/work knowledge is searchable and stored in rebuildable SQLite state;
- confirmed Markdown belongs only in the work vault after explicit human approval;
- personal-vault access remains task-scoped, on-demand, non-persistent, and excluded
  from all work-vault/index/draft paths;
- Paseo owns task/agent/turn lifecycle while the plugin remains a thin typed UI/RPC
  integration with no global composer interception;
- capture, idempotency, stale-source checks, atomic approval/recovery, provenance,
  and ordinary provider/session behavior are verified before acceptance.

Canonical design/plan:

`/Users/syang/.agentic-stack-kv-rpc/docs/plans/2026-09-13-agent-knowledge-vault-implementation.md`

Durable tracker:

`/Users/syang/.paseo/plans/agent-knowledge-vault.md`

Restart checkpoint:

`/Users/syang/.paseo/plans/agent-knowledge-vault-restart-checkpoint-20260919.md`

## Coordinator and child-agent identities

- Original coordinator: `18738a5c-ea5c-4527-9f9f-498950ad9860`
- Current/continuation manager: `dc8733cc-33c4-478f-a847-f788a945e9bd`
- Original independent auditor: `c66470dd-59a2-485e-873c-f3a4e1165fbc` — idle,
  report returned.
- Fresh continuation reviewer: `2cacc937-b403-4589-a646-78c8c01a785d` — no active
  turn; final concise verdict recovered below.
- Previous implementation/remediation workers `3903dbfa-6368-4774-b2ae-3e755a6650ea`,
  `6be8d0ef-6fa1-41dc-9ebe-e52931d602cb`, `70f80b94-fca9-4edc-9183-ab6e2ca3487a`,
  and `d6ddc193-dd55-4683-b76a-07c125d8b5f5` returned their bounded work; no child
  work remains in flight at checkpoint creation.

## Git, PR, and source state

- Canonical `master`: `afc1dfa`, clean, tracking `origin/master`.
- Continuation branch: `chore/knowledge-vault-remaining-acceptance` at `af2c471`,
  clean and pushed to the explicit fork remote.
- Open PR: **#46**, base `master`, head `chore/knowledge-vault-remaining-acceptance`,
  URL `https://github.com/syyangv/fork-agentic-stack/pull/46`.
- PR46 commits:
  - `227cb18` — fail-closed unsafe Knowledge Task starts, restore client cleanup,
    add Command Center panel access and adversarial/lifecycle tests.
  - `af2c471` — add optional `@rolldown/binding-darwin-arm64@1.2.8` lock entry.
- Installed plugin/CLI source path: `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault`
  at detached revision `444eec8`; preserve path and files. It has only preserved
  untracked `node_modules/` dirt. It has not been edited or committed by this lane.

## Dirty/untracked inventory

- Canonical master worktree: clean.
- Acceptance worktree: committed branch state clean before this checkpoint; this
  checkpoint file is the requested new untracked evidence file until deliberately
  delivered. No generated runtime files are staged.
- Acceptance runtime artifacts remain preserved and locally excluded:
  `.playwright-mcp/` and `integrations/paseo-knowledge/node_modules/`.
- Installed source retains untracked `node_modules/`; never stage or delete it.
- No personal-vault files, work-vault Markdown, credentials, or secrets were read or
  modified.

## Acceptance checklist and status

### P0 — compatibility/contracts: **PARTIAL/OPEN**

- Synthetic contracts, installed RPC dispatch, schema rejection, allowlist rejection,
  and task identity fixtures pass.
- Same-origin root/RPC transport was previously proven, but installed UI rendering,
  panel actions, close/reopen, and recovery are not yet installed-runtime proof.

### P1 — scope/authorization: **PASS-SYNTHETIC**

- Work/personal path boundaries, task-scoped grants, canonical/symlink/traversal
  checks, and personal quarantine are covered synthetically.
- No personal-vault pilot is authorized or completed.

### P2 — parser/index: **PASS-SYNTHETIC**

- Markdown parsing, chunking, SQLite/FTS5 rebuildability, multilingual/duplicate/
  stale/delete cases are covered by the knowledge suite and synthetic evaluation.

### P3 — retrieval/context/injection: **PARTIAL**

- Retrieval, provenance, bounds, stale hashes, and source markers pass synthetically.
- PR46 now fails closed before provider/workspace/agent creation because Paseo 0.8
  exposes only MCP `toolPolicy.preapproved`, not a provider-agnostic deny-all/no-tools
  boundary. This is technical enforcement, but it means Knowledge Task start is
  intentionally unavailable until a safe runner/boundary exists.

### P4 — Paseo plugin: **PARTIAL/OPEN**

- PR46 fixes the client contribution cleanup contract and adds workspace/agent
  Command Center access.
- The installed source is still old `444eec8`, so the fix is not installed.
- Installed panel controls/actions and typed prepare/start-fail-closed behavior remain
  open. No plugin reload was performed after PR46.

### P5 — capture/drafts: **PASS-SYNTHETIC; LIVE DRAFT PRESERVED**

- Tasks A/B/C were recorded previously. Task C remains the parked draft:
  `draft_7235e61b1d5c6363191baf58eadceb8d43de56938960dff887989242a5ec7d9e`
- Proposed target:
  `00-Inbox/synthetic-e2e-bounded-context-rule-d6ba23281d90.md`
- Store status remains `draft`; no formal approval or work-vault write occurred.

### P6 — human approval/recovery: **PASS-SYNTHETIC; UI OPEN**

- Core approval/hash/journal/recovery tests pass synthetically.
- Installed UI review/apply, close/reopen/recovery, and final UI identity proof remain
  open. The parked draft must not be self-approved.

### P7 — integrated acceptance: **OPEN**

Required remaining proof:

1. Installed same-origin UI panel controls and typed `prepare`/`start` behavior.
2. Installed UI close/reopen/recovery with retained task/agent/turn identity.
3. Ordinary provider/session/composer compatibility with plugin enabled and disabled.
4. Fresh installed native dependency/runtime proof, not only temp-copy proof.
5. Final independent review of the exact PR46 head and criterion-level acceptance.
6. Data-layer event proof where required.

## Independent review result

Reviewer: `2cacc937-b403-4589-a646-78c8c01a785d`

Exact reviewed revision: PR46 head `af2c471` (commits `227cb18`, `af2c471`).

Recovered verdict:

```text
VERDICT=do-not-merge; HIGH=yes; ISOLATION=valid; UI=open; TESTS=open; GAPS=installed UI,reopen,disable-enable,native install,error persistence
```

The earlier criterion auditor `c66470dd-59a2-485e-873c-f3a4e1165fbc` reported:
3 PASS, 66 PASS-SYNTHETIC, 7 PARTIAL, 9 OPEN, 2 OVERSTATED, including the original
HIGH untrusted-source/tool-isolation finding. PR46 addresses the unsafe start path by
failing closed, but it does not convert installed UI/runtime gaps into acceptance.

## Tests and verification

Fresh PR46 verification:

- Fresh native x64 temp copy: `npm ci` passed; Vitest **2 files / 23 tests passed**;
  `npm run typecheck` passed.
- Fresh arm64-targeted temp copy:
  `npm ci --cpu=arm64 --os=darwin` installed
  `@rolldown/binding-darwin-arm64`; Vitest was not run under this host's x64 Node
  runtime because the arm64 native binary cannot execute there.
- Repository/knowledge historical baseline: pytest 487 passed / 1 skipped; knowledge
  unittest discovery 66 passed. These are not fresh post-PR46 full-repository proof.
- Ordinary synthetic provider/session checks returned exactly:
  `KV-ORDINARY-ENABLED-OK` and `KV-ORDINARY-DISABLED-OK`.

## Current plugin/environment diagnosis

Supported direct client check after the approved restoration:

```json
{
  "connected": true,
  "plugin": {"id":"paseo-knowledge","enabled":true,"status":"running"},
  "configured": false,
  "coreStatus": "unconfigured",
  "error": "Set PASEO_KNOWLEDGE_PYTHON_EXECUTABLE and PASEO_KNOWLEDGE_CLI to absolute paths."
}
```

Targeted environment evidence:

- LaunchAgent plist and loaded job definition contain all six knowledge variables.
- `launchctl` reports the LaunchAgent `spawn scheduled`, without the active daemon PID.
- Actual daemon chain is desktop-managed:
  `/Applications/Paseo.app/Contents/MacOS/Paseo` → `Paseo Supervisor` → `Paseo Daemon`.
- Targeted actual-process inspection found no `PASEO_KNOWLEDGE_*` variables.
- Paseo plugin runtime uses `fork()` with inherited `process.env`; plugin config has
  source/path/enabled only and no per-plugin environment map.

Conclusion: a plugin-only reload cannot repair this process-environment mismatch.
The likely fix requires starting the daemon under the LaunchAgent environment or an
equivalent desktop-daemon environment bridge. No daemon restart or desktop lifecycle
change is authorized in this checkpoint.

## Permissions and exclusions

Authorized/allowed in the current continuation:

- scoped commits/pushes and normal PR delivery after independent review;
- restore plugin enabled state;
- conditional PR46 merge and installed-source update/reload only after review and
  coordinator confirmation.

Explicitly not authorized here:

- any daemon restart/bootout/bootstrap or desktop daemon lifecycle change;
- merge before independent review says merge-ready;
- personal-vault reads/indexing, credential/secret access, formal-note approval,
  RPC/auth bypass, force push, hook/CI bypass, memory deletion, destructive cleanup,
  unrelated ref/worktree cleanup, or upstream pushes;
- editing/committing the detached installed source or staging `node_modules/`,
  `.playwright-mcp/`, or runtime data.

## Safe resume steps

1. Read this checkpoint and verify its SHA/line count before any restart request.
2. Confirm coordinator/child state and that no returned/active child work is lost.
3. Keep PR46 unmerged until the reviewer verdict is superseded by a fresh
   merge-ready review of exact head `af2c471`.
4. Obtain explicit coordinator authorization for the exact daemon/desktop lifecycle
   repair required to make the knowledge environment reach the actual daemon process.
5. If authorized, record a pre-action snapshot, perform only that scoped lifecycle
   repair, and verify direct `/ws` `configured=true/coreStatus=ready` before any UI
   test.
6. Only after configured/ready: merge PR46 normally, update the preserved installed
   source path to the merged revision without committing detached worktree state,
   reload the plugin, and verify panel prepare/start-fail-closed/reopen behavior.
7. Restore/verify plugin enabled state, run ordinary enabled/disabled synthetic tasks,
   update evidence/tracker, and retire refs only after safe ancestry checks.

No restart has been performed for this checkpoint. Coordinator confirmation of this
file's preservation is required before any future restart request.

## Environment repair proposal checkpoint — 2026-09-20

The read-only diagnosis and exact supported ownership-transition proposal are saved
at:

`/Users/syang/.agentic-stack-kv-rpc/docs/evidence/knowledge-vault-environment-repair-proposal-20260920.md`

The proposal is **not executed**. It recommends pausing Paseo Desktop's built-in
daemon management, then booting out and bootstrapping the existing LaunchAgent plist
as the sole owner because the plist already carries the six knowledge variables and
Paseo Desktop has no supported per-plugin environment bridge. The proposal contains
preflight, postconditions, rollback, expected interruption, and exact approval wording.

Next approval gate: coordinator must read back this checkpoint and the proposal,
account for active agents, then explicitly approve the exact ownership transition.
No restart, desktop quit/relaunch, plugin reload, installed-source update, merge,
vault access, or formal-note approval has occurred for this proposal.
