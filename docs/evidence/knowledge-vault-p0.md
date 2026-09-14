# Phase P0 Capability Verification Evidence: Agent Knowledge Vault

- **Date**: 2026-09-13
- **Phase**: P0 · verify · installed Paseo capabilities and contract feasibility
- **Verification Lead**: Antigravity (Gemini 3.8 Flash, High)
- **Worktree CWD**: `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault`
- **Design Plan**: `docs/plans/2026-09-13-agent-knowledge-vault-implementation.md`
- **Execution Tracker**: `/Users/syang/.paseo/plans/agent-knowledge-vault.md`

---

## 1. Version Identification & Evidence Grounding

### 1.1 Installed Runtime Observations
- **CLI Executable Path**: `/Users/syang/.local/bin/paseo`
- **Reported CLI Version**: `0.7.2` (via `paseo --version`)
- **Desktop Application Bundle**: `/Applications/Paseo.app`
- **Reported Desktop Version**: `0.7.2` (via `/Applications/Paseo.app/Contents/Info.plist`, CFBundleShortVersionString `0.7.2`)
- **Live Daemon Diagnostics Status**:
  - `curl -s http://127.0.0.1:6767/api/health` returned `"Direct IP access is not allowed"` (daemon enforces host header / host security isolation).
  - `paseo daemon status` failed with `EPERM: operation not permitted, open '/Users/syang/.paseo/config.json'` due to sandbox execution restrictions.
  - **Verification Boundary**: The active daemon was **not** directly inspected via live diagnostics, and no live plugin installation or live agent creation was executed. Reporting matching semver version numbers (`0.7.2`) between CLI, Desktop, and source is **not** proof that the running daemon was compiled from the exact source commit or that live plugin execution behaves identically to source definitions.

### 1.2 Local Paseo Source Repository
- **Source Path**: `/Users/syang/projects/paseo`
- **Manifest Version**: `0.7.2` (in `package.json:3`)
- **Git HEAD Commit**: `b52442a221e36be975cca83a31c9d5315a2a1590` (clean tree)
- **Scope of Source Evidence**: Source files provide structural feasibility of contracts, types, and SDK methods. Source contracts represent architectural intent and API definitions, distinguished from live runtime verification.

### 1.3 Local Python & SQLite Engine
- **Python Executable**: `/Users/syang/miniconda3/bin/python3`
- **Python Version**: `3.12.7` (Clang 14.0.6)
- **SQLite Version**: `3.45.3`
- **FTS5 Trigram Verification (Runtime Grounded)**:
  - Executed in-memory test: `CREATE VIRTUAL TABLE test_trigram USING fts5(content, tokenize="trigram")` -> Success.
  - Query behavior verified:
    - 3-character query (`MATCH "测试中"`) on `"测试中文检索能力"` returned 1 hit.
    - 4-character query (`MATCH "中文检索"`) returned 1 hit.
    - 2-character query returned 0 hits.
  - **Constraint Identified**: SQLite FTS5 trigram requires token queries $\ge 3$ characters. Short queries ($<3$ characters) must fall back to escaped literal substring scanning (`LIKE` or regex), validating Plan step P3.3.

### 1.4 Target Vault & State Path Verification
- **Target Work Vault Path**: `/Users/syang/obsidian/agent-knowledge` (verified: does not exist; path is unallocated).
- **Target Private State Directory**: `/Users/syang/.agent/knowledge` (verified: does not exist; unallocated).
- **Repository `.gitignore` Inspection**:
  - Current `.gitignore` does **not** exclude `.agent/knowledge/`.
  - **Risk / Action for Phase 1**: Implementer must ensure that private runtime SQLite databases, journals, and lockfiles are either placed outside the git tree (e.g. `~/.agent/knowledge/` in user home) or explicitly added to `.gitignore` before file creation.

---

## 2. Integration Seams: Source Contract Feasibility vs Runtime Proof

All contract definitions below are derived from source inspection at `/Users/syang/projects/paseo`.

### 2.1 Workspace Panel Seam
- **Source Contracts**:
  - [`PluginWorkspacePanelContribution`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L136-L144) (`packages/plugin/src/contracts.ts:136–144`).
  - [`PluginWorkspacePanelProps`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L99-L102) (`context: "workspace"`, `workspaceId: string`, `theme: PluginTheme`, `host`, `layout`, `navigation`).
  - [`PluginAgentPanelProps`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L104-L108) (`context: "agent"`, `workspaceId: string`, `agentId: string`, `theme`, `layout`, `navigation`).
  - Panel locations: `locations?: readonly ("workspace" | "explorer")[]` (`contracts.ts:96`).
- **Registration**: [`PluginContext.addWorkspacePanel`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L297), implemented in [`host.ts:71–73`](file:///Users/syang/projects/paseo/packages/plugin/src/host.ts#L71-L73).
- **Client Hooks**: [`useWorkspace(workspaceId, selector)`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L196) and [`useAgent(agentId, selector)`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L201).
- **Navigation**: Client-owned navigation via `navigation?.openAgent({ agentId })` and `navigation?.openWorkspace({ workspaceId })` (`contracts.ts:35–41`).
- **Feasibility Assessment**: The plugin can contribute a workspace or agent panel without modifying Paseo core.
- **Runtime Proof Status**: Source-only feasibility; live panel rendering was **not** tested on the active desktop app.

### 2.2 Typed RPC Seam
- **Source Contracts**:
  - Definition: [`defineRpc<InputSchema, OutputSchema>`](file:///Users/syang/projects/paseo/packages/plugin/src/rpc.ts#L20-L26) (`packages/plugin/src/rpc.ts:20–26`). Name validated by `/^[a-z][a-z0-9._-]*$/` (`rpc.ts:18`).
  - Server Handler: Registered via [`PluginContext.handle(contract, handler)`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L288-L294). Handler receives [`PluginHandlerContext`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L283-L285) containing `{ paseo: PaseoApi }`.
  - Client Invocation: `useRpc(contract)` hook (`public-docs/plugins/reference.md:723–731`).
  - Backend Runner: Server contributions execute in a Node.js daemon subprocess ([`plugin-process.ts:1–60`](file:///Users/syang/projects/paseo/packages/server/src/server/plugins/plugin-process.ts#L1-L60)).
- **Subprocess Invocation**: Node handlers can safely execute the Python CLI via `child_process.execFile` passing explicit argv arrays (`['/Users/syang/miniconda3/bin/python3', '/path/to/knowledge.py', ...]`) without shell parsing.
- **Runtime Proof Status**: Contract and typing feasibility verified from source; actual IPC execution between live daemon and plugin subprocess is **unverified** in live runtime.

### 2.3 Attachment Source Seam
- **Source Contracts**:
  - [`defineAttachmentSource`](file:///Users/syang/projects/paseo/packages/plugin/src/server.ts#L12-L16) (`packages/plugin/src/server.ts:12–16`).
  - [`PluginAttachmentSourceContribution`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L231-L237) (`contracts.ts:231–237`).
  - Payloads: [`PluginAttachmentItemSchema`](file:///Users/syang/projects/paseo/packages/plugin/src/attachments.ts#L4-L18) and [`PluginAttachmentSearchPayloadSchema`](file:///Users/syang/projects/paseo/packages/plugin/src/attachments.ts#L20-L25).
  - Registration: [`PluginContext.addAttachmentSource`](file:///Users/syang/projects/paseo/packages/plugin/src/contracts.ts#L300), implemented in [`host.ts:80–82`](file:///Users/syang/projects/paseo/packages/plugin/src/host.ts#L80-L82).
- **Behavior & Non-Interception**: The attachment source surfaces in the message composer search menu on demand. It returns a stable text snapshot in `item.text`. It does **not** intercept standard composer send actions or alter prompt pipelines globally.
- **Runtime Proof Status**: Source contract verified; composer UI interaction in live app is **unverified**.

### 2.4 Task Identity & Agent Creation
- **Source Contracts**:
  - Creation: [`workspace.agents.create(options)`](file:///Users/syang/projects/paseo/packages/client/src/index.ts#L140) and [`paseo.agents.create(options)`](file:///Users/syang/projects/paseo/packages/client/src/index.ts#L312) (`packages/client/src/index.ts:140, 312, 432–455`).
  - Options: [`PaseoAgentCreateOptions`](file:///Users/syang/projects/paseo/packages/client/src/index.ts#L196-L212) (`index.ts:196–212`):
    - `config: PaseoAgentConfig` (`provider`, `modeId?`, `thinkingOptionId?`, `systemPrompt?`)
    - `labels?: Record<string, string>` (key-value labels attached to agent record)
    - `clientMessageId?: string` (request idempotency key)
- **Identity Binding Feasibility**: The plugin can attach `{ "knowledge_task_id": taskId, "vault_scope": "work" }` via `labels`.
- **Runtime Proof Status**: Supported in SDK and protocol contracts; creating live agents was **not** performed during P0.

### 2.5 Timeline, Turn Completion & Recovery Mechanics
- **Source Contracts**:
  - Active turn handle: [`PaseoAgentHandle.activeTurn`](file:///Users/syang/projects/paseo/packages/client/src/index.ts#L282) (`{ turnId, startedAt }`).
  - Turn wait method: [`PaseoAgentHandle.waitForFinish(timeoutMs?: number)`](file:///Users/syang/projects/paseo/packages/client/src/index.ts#L295) (`index.ts:295, 680–683`).
  - Timeline refetch: [`PaseoAgentTimelineHandle.refetch(options?)`](file:///Users/syang/projects/paseo/packages/client/src/index.ts#L259) (`index.ts:259, 685–692`).
  - Stream events: [`AgentStreamEvent`](file:///Users/syang/projects/paseo/packages/protocol/src/agent-types.ts#L359-L410) (`packages/protocol/src/agent-types.ts:359–410`) and [`getAgentStreamEventTurnId`](file:///Users/syang/projects/paseo/packages/protocol/src/agent-types.ts#L412).
- **Recovery Architecture (Source Feasibility)**:
  - If the UI client closes, an agent continues running in the daemon.
  - On reopen, the client can resolve the agent via `paseo.agents.ref(savedAgentId)`, call `agent.refresh()`, and call `agent.timeline.refetch({ limit: 10, direction: "backward" })` to inspect the terminal turn and parse candidate knowledge blocks.
- **Runtime Proof Status**: Recovery capability is grounded in `@getpaseo/client` SDK method availability in source. **Not verified end-to-end against a running daemon** due to authorization gates and sandboxed diagnostic constraints.

### 2.6 Model Route Reuse Feasibility
- **Policy Owner**: `~/.agent/skills/model-routing/SKILL.md` defines canonical route resolution for roles (`planning`, `research`, `impl`, `ui`, `audit`/`review`).
- **SDK Discovery APIs**:
  - `paseo.providers.listAvailable()` (`packages/client/src/index.ts:368`)
  - `paseo.providers.listModels(provider)` (`packages/client/src/index.ts:356`)
  - `paseo.providers.snapshot()` (`packages/client/src/index.ts:369`)
- **Feasibility Assessment**: The plugin does not need custom routing logic; it can discover installed providers or consume canonical routes from `$model-routing` and pass `config.provider: "<provider>/<model>"` with `config.thinkingOptionId`.

---

## 3. Grant-Lifetime Boundary & Architecture Constraint

A critical architectural boundary was identified for Phase 1 and Phase 4:

1. **Subprocess Ephemerality**:
   - In Paseo's plugin model, the daemon hosts a long-lived Node.js plugin subprocess (`plugin-process.ts`).
   - If typed RPC handlers invoke the Python CLI (`knowledge.py`) via transient subprocesses (`child_process.execFile`), each Python invocation terminates upon exit.
   - Therefore, transient Python CLI process memory **cannot maintain cross-request personal grant state**.
2. **Mandated Ownership**:
   - Task-scoped personal grants must be owned by the **long-lived Node.js plugin process** (or a dedicated long-lived Python core service in memory).
   - The long-lived process holds the in-memory map of `{ taskId: Set<grantedNotePaths> }`.
   - On each CLI invocation for that task, authorized note paths must be passed dynamically via argv or stdin.
3. **Strict Disk Quarantine**:
   - Personal grants and personal note paths must **never be persisted to disk, SQLite, or search indexes**.
   - When the task ends, or when the plugin process reloads/terminates, all in-memory grant state immediately vanishes.

---

## 4. Frozen Synthetic Contract Fixtures

All fixtures in `tests/fixtures/knowledge-paseo/` use strictly synthetic identifiers (`ktask_synthetic_p0_001`, `wks_synthetic_p0_001`, etc.), mathematically consistent content hashes, and character counts.

### 4.1 `SourceRef` ([source_ref.json](file:///Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/tests/fixtures/knowledge-paseo/source_ref.json))
- **Constraint**: `SourceRef` identifies provenance only; **does not contain a `text` field**.
- Fields: `source_id`, `vault_id`, `note_id`, `path`, `heading`, `line_start`, `line_end`, `content_hash`.
- `source_id` is the stable SHA-256 identity of `vault_id + NUL + note_id`; the synthetic fixture value is `1f247527f6bbada5950e276859b1f888b1b0f135915e87070c903dbb044ebf04`.
- Real Content SHA-256: `69a4bf304119ef5aa7d9ad07c5ad445b8528ce157111722103ad31a3aedceb63`.

### 4.2 `ContextBundle` ([context_bundle.json](file:///Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/tests/fixtures/knowledge-paseo/context_bundle.json))
- **Separation**: Holds `sources` (list of `SourceRef`, no text) and `snippets` (excerpt text with location) separately.
- Deterministic synthetic excerpt length: exactly 168 characters (`total_chars: 168`).
- Flags: `has_personal_context: false`, `truncated: false`.

### 4.3 `TaskRecord` ([task_record.json](file:///Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/tests/fixtures/knowledge-paseo/task_record.json))
- **Constraint**: `source_manifest` is an array of `SourceRef` objects and **never embeds text**.
- Identifiers: `task_id: "ktask_synthetic_p0_001"`, `workspace_id: "wks_synthetic_p0_001"`.

### 4.4 `KnowledgeDraft` ([knowledge_draft.json](file:///Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/tests/fixtures/knowledge-paseo/knowledge_draft.json))
- **Constraint**: Validation level is set to `"agent_reported"`. (It cannot be marked `verified_reproducible` without external observable verification evidence).
- **Current schema field**: Source provenance is stored in `source_refs`; the obsolete `sources` field is not used.
- Computed Draft SHA-256: `c9b2d88a055f30b9368c48ff41b98f745a874df35412a500935152fd8cf68be5`.

### 4.5 Consolidated Bundle ([contracts.json](file:///Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/tests/fixtures/knowledge-paseo/contracts.json))
- Combines all four validated schemas into a single validated JSON structure.

---

## 5. Summary Matrix & Acceptance Checklist

| Requirement / Item | Evidence Status | Grounding Level |
|---|---|---|
| Paseo CLI Version | `0.7.2` | Installed Runtime (`/Users/syang/.local/bin/paseo`) |
| Paseo Desktop Version | `0.7.2` | Installed Runtime (`/Applications/Paseo.app`) |
| Python 3.12.7 & SQLite 3.45.3 | Confirmed | Installed Runtime (`/Users/syang/miniconda3/bin/python3`) |
| SQLite FTS5 Trigram Tokenizer | Confirmed ($\ge 3$ chars) | Installed Runtime (in-memory SQLite) |
| Target Vault Path Availability | Confirmed unallocated | Installed Runtime (`/Users/syang/obsidian/agent-knowledge`) |
| Workspace Panel Contracts | Verified lines 99–144 | Source Feasibility (`packages/plugin/src/contracts.ts`) |
| Typed RPC Contracts | Verified lines 20–26 | Source Feasibility (`packages/plugin/src/rpc.ts`) |
| Attachment Source Contracts | Verified lines 231–237 | Source Feasibility (`packages/plugin/src/contracts.ts`) |
| Task Labels & Idempotency | Verified lines 196–212 | Source Feasibility (`packages/client/src/index.ts`) |
| Offline Turn Recovery Mechanics | Verified lines 259–295 | Source Feasibility (`packages/client/src/index.ts`) |
| Grant-Lifetime Boundary | Documented | Architecture Requirement |
| Synthetic Contract Fixtures | Generated & validated | Deterministic Synthetic Data |
| Live Daemon Health / Status | **Unverified / Blocked** | Blocked by sandbox `EPERM` on `~/.paseo/config.json` |
| End-to-End Live Recovery | **Unverified / Gated** | Gated; no live agents spawned in P0 |
| Live Plugin Installation | **Unverified / Gated** | Gated by Phase 8 explicit user authorization |
