# Paseo Knowledge Task plugin (P4)

This is a thin, source-contract-targeted Paseo plugin scaffold. It is not
installed or connected by this repository change.

## Runtime boundary

- `panel.client.tsx` contains only React Native UI, cached workspace/agent
  selectors, and typed RPC calls. It never reads a vault or creates a client.
- `service.server.ts` owns task state, task-scoped personal grants, agent
  creation, explicit send, lifecycle subscriptions, timeline refetch, and
  reconciliation.
- `core.server.ts` invokes the versioned Python CLI with
  `spawn(absolutePythonExecutable, argv, { shell: false })`. Core JSON remains
  stdout; stderr is diagnostics and is never treated as a result.
- `contracts.shared.ts` defines the typed RPC and attachment seams.

Configure the daemon-side environment with absolute paths before a separately
authorized installation:

```text
PASEO_KNOWLEDGE_PYTHON_EXECUTABLE=/absolute/path/to/python3
PASEO_KNOWLEDGE_CLI=/absolute/path/to/.agent/tools/knowledge.py
PASEO_KNOWLEDGE_WORK_VAULT=/absolute/path/to/agent-knowledge
PASEO_KNOWLEDGE_PERSONAL_VAULT=/absolute/path/to/personal-vault
PASEO_KNOWLEDGE_RUNTIME_DIR=/absolute/path/to/private/runtime
PASEO_KNOWLEDGE_DRAFT_STORE=/absolute/path/to/private/runtime/drafts.sqlite3
PASEO_KNOWLEDGE_TASK_STATE=/absolute/path/to/private/runtime/task-state.json
```

`PASEO_KNOWLEDGE_DRAFT_STORE` is the private P5 draft database path. If it is
omitted, the plugin derives `drafts.sqlite3` beneath the configured runtime
directory; without either path, verified completion remains
`capture_incomplete` rather than creating an implicit store.

`PASEO_KNOWLEDGE_TASK_STATE` is the private durable task-record path used for
restart/reopen reconciliation. If it is omitted, the plugin uses
`task-state.json` beneath `PASEO_KNOWLEDGE_RUNTIME_DIR`, or defaults to
`~/.agent/knowledge/task-state.json`. The file is atomically replaced with
restrictive permissions and contains only bounded task identity, status,
timestamps, work provenance, exclusions, capture status, and bounded errors.
Context, session output, personal excerpts, and personal grant paths remain
process-memory-only. This file is private runtime state, not an OS sandbox.

The provider input is required to be the caller-selected Paseo
`provider/model` string. The plugin does not choose a provider or model.

## Personal-source limitation

The checked-in Python CLI has no verified transport for task-scoped personal
grants. The plugin therefore retains grants only in memory and refuses a
personal search/context call rather than performing an unscoped read. A future
supervised core seam may consume the ephemeral grant list via stdin after its
contract is verified. No grant paths or personal excerpts are persisted.

## Lifecycle and safety

Create and send are separate. Stable `clientMessageId`, message ID, task ID,
agent ID, and turn ID prevent an ambiguous result from causing an automatic
resend. Timeout marks the task as timed out while retaining the last known
identity. Closing the panel does not cancel the Paseo agent. There is no formal
write RPC, agent-facing write tool, or global composer interception.

The tests use synthetic core payloads and fake Paseo SDK objects at the plugin
boundary. They do not require a live daemon, dependency installation, or a
personal vault.
