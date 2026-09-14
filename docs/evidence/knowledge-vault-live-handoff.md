# Agent Knowledge Vault — Live Handoff and Authorization Package

Date: 2026-09-14
Worktree: `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault`
Branch: `feat/agent-knowledge-vault`

## Authorized and completed

The user explicitly authorized initialization of the independent work vault and private runtime, plus plugin installation and necessary Paseo reload. No personal-vault read or index was authorized.

Completed safely after confirming both destinations were absent:

```text
Work vault: /Users/syang/obsidian/agent-knowledge
Runtime:    /Users/syang/.agent/knowledge
```

Initialization command:

```sh
/Users/syang/miniconda3/bin/python3 .agent/tools/knowledge.py init \
  --work-vault /Users/syang/obsidian/agent-knowledge \
  --runtime-dir /Users/syang/.agent/knowledge
```

Result: created `Home.md`, `00-Inbox/`, the four formal directories, `90-Archive/`, `_templates/`, and `00-Inbox/README.md`; skipped 0 files. The runtime directory is empty pending first plugin/core use.

## Plugin destination and mechanism

Source reviewed and typechecked:

```text
/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/integrations/paseo-knowledge
runtime ID: paseo-knowledge
Implementation source provenance: `449957125718e822020ad664d59a77b2a975df61`; merged into fork `master` as merge commit `a742258d923b920252057f4a0bf194df74f74098`.
```

Install command used:

```sh
/Users/syang/.local/bin/paseo plugin install \
  /Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/integrations/paseo-knowledge
```

Paseo stages/registers the trusted plugin for the local daemon; it does not delete the source directory. The plugin has no custom build step. Local pre-install checks passed: TypeScript and 21 synthetic Vitest tests.

Required daemon-side environment values are documented, not applied here:

```text
PASEO_KNOWLEDGE_PYTHON_EXECUTABLE=/Users/syang/miniconda3/bin/python3
PASEO_KNOWLEDGE_CLI=/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/.agent/tools/knowledge.py
PASEO_KNOWLEDGE_WORK_VAULT=/Users/syang/obsidian/agent-knowledge
PASEO_KNOWLEDGE_RUNTIME_DIR=/Users/syang/.agent/knowledge
PASEO_KNOWLEDGE_DRAFT_STORE=/Users/syang/.agent/knowledge/drafts.sqlite3
PASEO_KNOWLEDGE_TASK_STATE=/Users/syang/.agent/knowledge/task-state.json
```

`PASEO_KNOWLEDGE_PERSONAL_VAULT` is intentionally not configured for this smoke path.

## Live installation result

Paseo daemon status before installation reported local/connected daemon version `0.7.2`. The plugin catalog was empty and root `pluginsEnabled` was absent. After the user's separate explicit trusted-plugin authorization, a permission-preserving backup was created at:

```text
/Users/syang/.paseo/config.json.pre-knowledge-plugin-20260914T134854Z
```

The only config change was root `pluginsEnabled: true`, followed by:

```text
set ~/.paseo/config.json: pluginsEnabled = true
/Users/syang/.local/bin/paseo reload --json
```

Reload result: `appliedPaths: ["pluginsEnabled"]`, `restartRequiredPaths: []`.

The plugin was installed with:

```sh
/Users/syang/.local/bin/paseo plugin install \
  /Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/integrations/paseo-knowledge
```

Health verification:

```text
paseo-knowledge: enabled=true, status=running
source=/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/integrations/paseo-knowledge
```

Plugin log: `Loading plugin`, `Plugin ready`; one Node `fs.F_OK` deprecation warning; no plugin load/RPC error observed.

The six absolute core variables were propagated through the persistent LaunchAgent and the daemon was restarted. The daemon PID changed from `62512` to `63079`, start time became `2026-09-14T14:22:56.906Z`, and the personal-vault variable remained absent.

For reference, the intended daemon-side environment is:

```text
PASEO_KNOWLEDGE_PYTHON_EXECUTABLE=/Users/syang/miniconda3/bin/python3
PASEO_KNOWLEDGE_CLI=/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/.agent/tools/knowledge.py
PASEO_KNOWLEDGE_WORK_VAULT=/Users/syang/obsidian/agent-knowledge
PASEO_KNOWLEDGE_RUNTIME_DIR=/Users/syang/.agent/knowledge
PASEO_KNOWLEDGE_DRAFT_STORE=/Users/syang/.agent/knowledge/drafts.sqlite3
PASEO_KNOWLEDGE_TASK_STATE=/Users/syang/.agent/knowledge/task-state.json
```

## Rollback and live smoke result

Rollback commands:

```sh
/Users/syang/.local/bin/paseo plugin disable paseo-knowledge
/Users/syang/.local/bin/paseo plugin remove paseo-knowledge
```

If the global switch must be disabled, restore the saved config backup or set `pluginsEnabled=false` and run `paseo reload --json`. The source directory and initialized work vault are not deleted automatically.

The production core subprocess boundary was exercised with the propagated absolute paths and synthetic work-vault data:

```text
status: ready
search: ok, 1 synthetic source result
build_context: ok, 328 bounded characters, personal_context_used=false
capture: draft_created (test-only task/turn)
review_apply: accepted, target 20-Research/live-smoke-accepted-20260914-c.md, index_updated=true
later search: formal accepted path returned; Inbox draft path excluded; no personal result
```

This proves the live Python/core subprocess path, draft store, explicit human approval, atomic apply, path-scoped index refresh, and later retrieval. Direct plugin UI/RPC invocation was not exercised because the Paseo browser automation host timed out; plugin process health was verified through `paseo plugin ls` and logs. No real provider task or personal source was used.

The original activation sequence was:

```sh
/Users/syang/.local/bin/paseo reload --json
/Users/syang/.local/bin/paseo plugin ls --json
```

The daemon-wide switch affects all configured trusted plugins; the user separately confirmed this exact change. The plugin is installed and running, but its core CLI environment is not yet propagated to the already-running daemon.

## Synthetic-only live smoke (pending daemon environment propagation)

1. If a direct plugin UI/RPC smoke is required, reconnect a supported Paseo browser/UI host and invoke only the same synthetic work scope.
2. Verify ordinary Paseo task/composer paths are not intercepted and inspect plugin logs for errors without exposing credentials.
3. Remove synthetic smoke notes only by explicit path-specific authorization; never bulk-delete or alter existing notes.

## Synthetic evidence and remaining gates

- Final Python knowledge suite: 62 passed.
- Final plugin suite: 21 passed; TypeScript and Python compilation passed.
- Repository suite: 449 passed, 1 skipped for native Windows junction support.
- P0 fixture invariants: 8 source refs, 168 context characters, matching draft hash, `agent_reported`.
- T18: explicit formal approval, path-scoped refresh, later retrieval, Inbox exclusion — passed synthetically.
- P7.2 20-query synthetic evaluation: 20/20 expected-source hits, 100% recall@5, 22/22 source-location correctness; evidence in `docs/evidence/knowledge-vault-p7-synthetic-evaluation.md`.

Verified live: daemon environment propagation, restart, plugin reload/health, plugin startup logs, and the production core subprocess capture/approval/retrieval loop. Direct plugin UI/RPC invocation remains unverified because the browser automation host was unavailable. Not verified or authorized: real provider task, personal-data pilot, real-data E2E, latency/performance pilot, 20-query real-data recall, deployment. Git commit/push/merge are tracked separately in the repository handoff.
