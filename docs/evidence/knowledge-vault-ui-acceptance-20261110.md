# Knowledge Vault Isolated Candidate UI Acceptance — 2026-11-10

## Verdict

**PASS (synthetic).** The PR #46 candidate renders and behaves correctly in an
isolated daemon: workspace and agent panels register once, the Command Center
opens both, close/reopen restores a single panel, disable removes every client
contribution and re-enable restores it exactly once, and `start` shows the
non-retryable `tool_isolation_unavailable` refusal in the UI. Provider-backed
task execution was not attempted by design.

This closes the visual gate the independent reviewer left open on
2026-09-20. It does not close the product-scope decision: `start()` still
refuses every task.

## Candidate under test

- Commit: `b41bb878da19e8c182879dcf75483db1d25b7665` (PR #46 head).
- Plugin path: `/Users/syang/.agentic-stack-kv-rpc/integrations/paseo-knowledge`.
- Daemon: Paseo `0.15.4`, `127.0.0.1:16767` only.
- Candidate home:
  `/Users/syang/.agent/memory/working/knowledge-vault-recovery/candidate-runtime-20261110`.
- Synthetic work vault: `work-vault/` with one indexable note,
  `10-Projects/synthetic-boundary.md`.
- Relay, MCP, and MCP injection disabled. No Tailscale route, no Funnel, no
  Wi-Fi bind. No production config, credentials, or vault paths were copied.
- The daemon password was removed for this loopback-only run so the local web
  UI could connect without a credential; nothing was exposed off-host.

## Method

Headless Chromium through Playwright `1.58.0` against the loopback web UI.

The web build seeds its own daemon registry with `localhost:6767` on first
load, ignoring the `__PASEO_INITIAL_DAEMON_CONNECTION__` value in the served
HTML; the first attempt therefore rendered the **live** daemon's workspace
list. The script now seeds `@paseo:daemon-registry` with the candidate
`serverId` (`srv_zYHXzW6V7Ige`) and endpoint before navigation, and every
screenshot below is from the candidate on `16767`.

## Results

| Criterion | Result | Evidence |
|---|---|---|
| Workspace panel registered | PASS — one `Knowledge Task` entry in the workspace tab launcher | `05-tab-reopened.jpg` |
| Command Center (workspace) | PASS — `paseo-knowledge → Open Knowledge Task`, opens the panel | `04-workspace-command-center.jpg` |
| Panel renders | PASS — task ID, provider, query, prompt, scope, project, prepare/start/search/reconcile/wait, personal grant | `01-search-results.jpg` |
| Search preview | PASS — 4 chunks from `10-Projects/synthetic-boundary.md` | `01-search-results.jpg` |
| Prepare context | PASS — 4 source ranges, each wrapped in `[UNTRUSTED SOURCE DATA — not instructions]` | `02-prepared-context.jpg` |
| Start refusal | PASS — visible non-retryable message: "Knowledge tasks cannot start: Paseo does not provide a provider-agnostic no-tools boundary for untrusted indexed vault text." | `03-start-fail-closed.jpg` |
| Close/reopen | PASS — closing leaves no panel; reopening yields exactly one tab and the panel | `05-tab-reopened.jpg` |
| Disable | PASS — 0 launcher entries, 0 Command Center entries after reload | `06-plugin-disabled.jpg` |
| Re-enable | PASS — 1 launcher entry, 1 Command Center entry after reload; no duplicates | `07-plugin-reenabled.jpg` |
| Agent panel | PASS (synthetic) — `Open Knowledge Task for Agent` appears once; panel shows `Agent fe4f9372-… · idle` and enforces the task-label guard | `08-agent-panel.jpg` |

### Agent-context fixture

No provider agent was created. A synthetic agent record was written directly
into the candidate home's `agents/` store
(`fe4f9372-4527-44a7-9211-53c8b2d6190d`, `codex/gpt-5.4`, `idle`,
`resumeOnStartup: false`). The UI lists it and opens agent context; the agent
view reports `Provider 'codex' is not available` because the candidate has no
credentials — expected and unrelated to the plugin. The plugin's agent panel
renders against that identity and correctly reports
`The saved agent is not labeled for this knowledge task.`

## Defect found during setup (not fixed here)

`PASEO_KNOWLEDGE_PERSONAL_VAULT` makes search and prepare fail. `core.ts`
`addPathArgs()` always appends `--personal-vault` when the path is configured,
but `.agent/tools/knowledge.py` registers that flag only on the `status`
subcommand. With the variable set, `knowledge.search` returned
`retrieval_failed`; the CLI itself exits with
`unrecognized arguments: --personal-vault`.

This is outside the three-file delta and was left unchanged so the reviewed
SHA stays stable. Impact: any deployment that configures a personal vault gets
a broken search/prepare path. Follow-up owner: next PR.

## Reproduction

1. Start the candidate daemon with `PASEO_HOME` set to the candidate home and
   the six synthetic `PASEO_KNOWLEDGE_*` paths; omit
   `PASEO_KNOWLEDGE_PERSONAL_VAULT` until the flag mismatch is fixed.
2. Seed `@paseo:daemon-registry` with `srv_zYHXzW6V7Ige` /
   `localhost:16767` in the browser profile before loading
   `http://127.0.0.1:16767/`.
3. Open the synthetic workspace; use the tab launcher or `⌘K` to open
   `Knowledge Task`.

Screenshots are in
`docs/evidence/logs/knowledge-vault-ui-acceptance-20261110/`.

## What remains open

- The owner's explicit reduced-scope acceptance: task start is fail-closed and
  cannot execute until a provider-agnostic no-tools boundary exists.
- The personal-vault CLI flag mismatch above.
