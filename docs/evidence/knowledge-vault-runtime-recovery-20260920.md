# Knowledge Vault — Runtime Recovery & LaunchAgent Transition Evidence

**Date:** 2026-09-20  
**Target Document:** `/Users/syang/.agentic-stack-kv-rpc/docs/evidence/knowledge-vault-runtime-recovery-20260920.md`  
**Purpose:** Record the observed successful transition of the Paseo daemon to LaunchAgent ownership (`com.syang.paseo.daemon`), verify runtime core-readiness, note the historical status of the earlier environment blocker, and enumerate remaining candidate verification, UI, and review gaps.

---

## 1. Executive Summary

On 2026-09-20, the v2 transition runner (`v2-live-20260920T191624Z`) completed a verified, live transition of the Paseo daemon from built-in desktop management to the pre-configured LaunchAgent (`com.syang.paseo.daemon`). 

- **Runtime State:** The daemon is actively running under LaunchAgent supervision (supervisor PID `97428`, listener PID `97434` on `127.0.0.1:6767`). Direct RPC confirms `connected=true`, plugin `paseo-knowledge` `enabled=true`/`status=running`, and `configured=true`/`coreStatus=ready`.
- **Blocker Status:** The previous environment mismatch documented in `knowledge-vault-recovery-checkpoint.md` (where the desktop-spawned daemon lacked the six `PASEO_KNOWLEDGE_*` environment variables, resulting in `configured=false`) is **historical and resolved**.
- **Scope & Gaps:** No daemon re-transition is needed. However, this transition **does not** constitute full acceptance, candidate test completion, or PR46 approval. Branch `chore/knowledge-vault-remaining-acceptance` (PR #46, HEAD `af2c471`) remains unmerged under an active independent reviewer `do-not-merge` gate. Installed UI, candidate testing, safe-execution boundary, and post-merge installed verification remain open.

---

## 2. Source Files & Cryptographic Hashes

| Source File | Description | SHA256 Hash |
| :--- | :--- | :--- |
| `/Users/syang/.agent/memory/working/knowledge-vault-recovery/coordinator-handoff.md` | Coordinator handoff & execution record | `4eca209722d643e44e8c6d2ef0da2b2ad75af34e7d6d4985d84d0f19857a4f42` |
| `/Users/syang/.agent/memory/working/knowledge-vault-recovery/v2-live-20260920T191624Z/output/status.json` | Transition runner final status output | `6e3f07d64631be7aa594101167fe8266c20774e6c3554ac5e8f6c97e08d666c6` |
| `/Users/syang/.agent/memory/working/knowledge-vault-recovery/v2-live-20260920T191624Z/output/events.jsonl` | Sequential event timeline of transition | `1e7017a5d2683e5f6208d294a31f0c7500fb2b9eef3b17c637aa555e55ffd123` |
| `/Users/syang/.agentic-stack-kv-rpc/docs/evidence/knowledge-vault-recovery-checkpoint.md` | Previous recovery checkpoint (historical environment blocker) | `a08da1f4c1cf6f9713b025a4d4a3b44486a133f72bd032d6acffcdd4bde0642a` |

---

## 3. Observed LaunchAgent Transition Details

### 3.1 Transition Run Profile
- **Run Directory:** `/Users/syang/.agent/memory/working/knowledge-vault-recovery/v2-live-20260920T191624Z`
- **Initial State:** Desktop-managed listener PID `52150` (parent `52149`), LaunchAgent `com.syang.paseo.daemon` not registered.
- **Transition Helper:** State-aware `transition-helper-v2` (22 unit tests passing, independent code-review approved, manual recovery fallback contract).

### 3.2 Chronological Event Log (`output/events.jsonl`)
1. `preflight` (`unix_time: 1789931812.945258`):  
   `registered: false`, `job_pid: null`, `listener_pids: [52150]`, `mutation_allowed: true`
2. `waiting_for_desktop_shutdown` (`unix_time: 1789931812.945895`):  
   Awaiting clean shutdown of desktop daemon listener.
3. `bootstrap_submitted` (`unix_time: 1789931851.712057`):  
   LaunchAgent bootstrap submitted to `launchd` (`gui/501`).
4. `waiting_for_launchagent_listener` (`unix_time: 1789931851.713109`):  
   Awaiting LaunchAgent listener socket activation.
5. `rpc_verification` (`unix_time: 1789931855.581571`):  
   `connected: true`, `enabled: true`, `plugin_status: "running"`, `rpc_ok: true`, `configured: true`, `core_status: "ready"`
6. `verified_core_ready` (`unix_time: 1789931855.632728`):  
   Final status confirmed with `job_pid: 97428` and `listener_pids: [97434]`.

### 3.3 Post-Transition Live Reverification
As confirmed by the replacement coordinator inspection recorded in `coordinator-handoff.md`:
- Registered LaunchAgent supervisor PID: `97428`
- Child listener PID: `97434` bound to `127.0.0.1:6767`
- RPC response via supported direct interface:
  ```json
  {
    "connected": true,
    "plugin": {
      "id": "paseo-knowledge",
      "enabled": true,
      "status": "running"
    },
    "configured": true,
    "coreStatus": "ready"
  }
  ```
- No competing or delegated worker tasks were running at verification; coordinator ID `e6f626c3-ef9c-44f4-8394-025800f8bb78` was the sole active session.

---

## 4. Current State vs. Historical Checkpoint

| Dimension | Historical Checkpoint (`knowledge-vault-recovery-checkpoint.md`) | Current Recovered State (This Evidence) |
| :--- | :--- | :--- |
| **Daemon Ownership** | Desktop-managed (`/Applications/Paseo.app/...` → Supervisor → Daemon) | Sole LaunchAgent ownership (`com.syang.paseo.daemon`, supervisor PID 97428) |
| **Environment Injection** | Desktop process lacked `PASEO_KNOWLEDGE_*` variables | Inherited from LaunchAgent plist containing all 6 knowledge variables |
| **Plugin Core Status** | `configured: false`, `coreStatus: "unconfigured"`, error requesting executable/cli paths | `configured: true`, `coreStatus: "ready"`, `error: null` |
| **Environment Blocker** | **ACTIVE BLOCKER** preventing plugin-level execution | **HISTORICAL & RESOLVED**; do not repeat ownership transition |

---

## 5. Remaining Acceptance & Candidate Gaps

Resolving the LaunchAgent environment blocker enables subsequent candidate verification, but does **not** satisfy candidate acceptance criteria. The following gates and gaps remain:

### 5.1 Independent Reviewer & PR Status
- **Reviewer:** `2cacc937-b403-4589-a646-78c8c01a785d`
- **Reviewed Head:** PR #46 (`af2c471`, branch `chore/knowledge-vault-remaining-acceptance`)
- **Current Verdict:** `VERDICT=do-not-merge; HIGH=yes; ISOLATION=valid; UI=open; TESTS=open; GAPS=installed UI,reopen,disable-enable,native install,error persistence`
- **Constraint:** PR #46 must **not** be merged until an updated, explicit merge-ready verdict is rendered by the independent reviewer on the final candidate SHA.

### 5.2 Installed Source vs. Candidate Source
- The live installed source at `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault` remains at detached revision `444eec8` with untracked `node_modules/`.
- Candidate changes in PR #46 (commits `227cb18` and `af2c471`) are not yet installed in the live runtime.
- Pre-merge testing must follow the agreed isolated candidate test sequencing (Step 5 in execution plan) to avoid circular merge dependencies or unauthorized mutation of installed checkout.

### 5.3 Safe-Execution & Isolation Boundary (P3 / Step 6)
- PR #46 implements a fail-closed check for Knowledge Task starts because Paseo 0.8 only exposes `toolPolicy.preapproved`, rather than a true provider-agnostic deny-all/no-tools execution sandbox.
- While fail-closed behavior satisfies safety by preventing insecure starts, functional Knowledge Task execution remains open until either a supported restricted execution runner is verified or the user approves a scoped, clearly labeled feature reduction.

### 5.4 Installed UI & Panel Integration (P0, P4, P6, P7)
- Panel controls and Command Center access in workspace/agent views are unverified in the installed runtime.
- Close, reopen, and reconnect recovery with retained task/agent/turn identity remain open.
- Error persistence and error presentation in the installed UI remain unverified.
- Ordinary path verification (composer/provider compatibility with plugin enabled and disabled) has only synthetic tokens (`KV-ORDINARY-ENABLED-OK`, `KV-ORDINARY-DISABLED-OK`), not full installed UI proof.

### 5.5 Data Layer & Vault Safety
- **Parked Draft:** Draft `draft_7235e61b1d5c6363191baf58eadceb8d43de56938960dff887989242a5ec7d9e` remains safely parked in draft storage targeting `00-Inbox/synthetic-e2e-bounded-context-rule-d6ba23281d90.md`. It must not be self-approved or written to the work vault without explicit human action.
- **Personal Vault Exclusion:** Personal vault access remains strictly excluded, with no file reads, writes, or indexing authorized or executed.

---

## 6. Next Steps & Operating Directives

1. **Do Not Retouch Daemon Ownership:** The LaunchAgent transition is verified and complete. Do not reload, restart, bootstrap, or bootout the daemon.
2. **Follow Authoritative Execution Plan:** Proceed to Step 5 (candidate verification sequencing) and Step 6 (safe execution gap assessment) as defined in `coordinator-handoff.md`.
3. **Respect Review & Merge Gates:** Retain PR #46 unmerged until independent re-review explicitly approves merge.
4. **Preserve State Integrity:** Maintain isolation of installed source `444eec8` and keep personal vaults strictly excluded.
