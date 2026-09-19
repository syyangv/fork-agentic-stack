# Knowledge Vault — Live Plugin RPC and Client Connectivity Diagnosis

Date: 2026-09-19
Daemon: Paseo `0.8.0`, `serverId srv_JQmi-U1LUQJa`, listening `127.0.0.1:6767`
Installed plugin source: `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/integrations/paseo-knowledge`
Scope: read-only diagnosis plus one new repository verification tool. No daemon restart, no config change, no personal-vault access, no credential access, no RPC/auth bypass.

## 1. Client-to-daemon connectivity: root cause

The previously recorded blocker was a hosted Paseo web client whose Direct connection to
`tcp://127.0.0.1:6767` and `tcp://localhost:6767` timed out with close code `1006`, while
`/api/health` returned `status ok`. That symptom was read as a daemon transport failure. It is not.

Measured against the running daemon:

| Probe | Result |
|---|---|
| `GET /api/health` | `200`, `{"status":"ok",...}` |
| `GET /` | `404`, zero bytes |
| WebSocket upgrade to `/` | `400 Bad Request` |
| WebSocket upgrade to `/ws` | `101 Switching Protocols`, valid `Sec-WebSocket-Accept` |
| `paseo --json plugin ls` | exit `0`, live payload |

The daemon's WebSocket endpoint is `/ws` and it is healthy. `daemon.log` shows an active
`websocket-server` with four live connections, `originRejected: 0`, `hostRejected: 0`, and
served request types including `plugin.catalog.get.request`.

The hosted client failed for a client-side reason, not a daemon fault: the hosted app is served
from a remote HTTPS origin and cannot open a plaintext `ws://127.0.0.1:6767` socket back to
loopback. The supported remedy documented in Paseo's own `public-docs/web-ui.md` is to serve the
bundled web app from the daemon itself, so the page, the API, and the WebSocket share one origin:

> The page is served from the same origin as the daemon's API and WebSocket. When you open it, the
> app automatically connects back to that same origin, so you usually skip the "Add Host" step
> entirely.

Root `/` currently returns `404` because the bundled web UI is disabled. See section 4.

## 2. Live installed-plugin RPC dispatch: PROVEN

`@getpaseo/client@0.8.0` — already a pinned devDependency of this plugin and version-matched to the
running daemon — exposes `DaemonClient.invokePluginRpc(pluginId, method, input)`. That is the same
supported `plugin.rpc.invoke` protocol path the plugin's own UI panels use through `useRpc()`.
Using it is a supported client call, not an internal bypass.

New tool: [`integrations/paseo-knowledge/tools/live-rpc-probe.mjs`](../../integrations/paseo-knowledge/tools/live-rpc-probe.mjs).

Results against the installed, enabled, running plugin:

| RPC | Outcome | Evidence value |
|---|---|---|
| `knowledge.status` | dispatched, typed response in `1.1–44.0 ms` | live handler dispatch |
| `knowledge.search` | dispatched, typed response, `sourceContentUntrusted: true` | injection-isolation flag present at runtime |
| `knowledge.attachments.search` | dispatched, returned `{"items":[]}` | attachment source live and answering |
| `knowledge.task.prepare` | rejected server-side by schema: missing `workspaceId`, `provider` | typed input contract enforced live |
| `knowledge.bogus.method` | rejected: `Plugin paseo-knowledge does not contribute RPC knowledge.bogus.method` | daemon enforces the contributed-RPC allowlist |

`listPlugins()` concurrently reported `paseo-knowledge enabled=true status=running`.

This closes the transport, dispatch, contract-validation, and allowlist-enforcement portions of the
live plugin RPC criterion. It does **not** close functional task flow; see section 3.

## 3. New blocker found: daemon process is missing the knowledge environment

`knowledge.status` returned, from inside the daemon process:

```json
{"configured": false, "coreStatus": "unconfigured",
 "error": {"code": "configuration",
           "message": "Set PASEO_KNOWLEDGE_PYTHON_EXECUTABLE and PASEO_KNOWLEDGE_CLI to absolute paths.",
           "retryable": false}}
```

`server/core.ts` resolves configuration exclusively from `process.env` via
`KnowledgeCliRunner.fromEnvironment(process.env)`, so the running daemon has no
`PASEO_KNOWLEDGE_*` values.

The cause is a stale launchd job definition, not a missing configuration file. The LaunchAgent
plist at `~/Library/LaunchAgents/com.syang.paseo.daemon.plist` (modified 2026-09-14 10:22) **does**
contain all six intended variables. The *loaded* job definition does not:

```text
$ launchctl print gui/501/com.syang.paseo.daemon
  environment = {
    PATH => ...
    HOME => /Users/syang
    PASEO_HOME => /Users/syang/.paseo
    XPC_SERVICE_NAME => com.syang.paseo.daemon
  }
```

launchd caches a job's definition at bootstrap time. The plist was edited after the job was already
loaded, and every `keepalive` respawn since has reused the stale definition. Editing the plist alone
cannot fix this; the job must be booted out and bootstrapped again.

This is the reason no functional status/search/prepare/context/review flow can run over RPC today,
and it supersedes the earlier assumption that daemon environment propagation was already delivered.

## 4. Bundled web UI is not hot-reloadable

The daemon's reload handler applies only the paths in `RELOADABLE_PATHS`:

```text
daemon.relay.enabled, daemon.mcp.enabled, daemon.mcp.injectIntoAgents,
daemon.browserTools.enabled, daemon.hostnames, daemon.cors.allowedOrigins,
daemon.trustedProxies, daemon.git.maxProcessesPerSecond, daemon.git.maxProcessConcurrency,
daemon.autoArchiveAfterMerge, daemon.enableTerminalAgentHooks, daemon.appendSystemPrompt,
daemon.terminalProfiles, daemon.agentProfiles, app.baseUrl, agents.providers,
agents.catalogRefreshTimeoutMs, agents.metadataGeneration, agents.skills.selection, pluginsEnabled
```

`features.webUi.enabled` is absent. `mountWebUi()` reads `config.webUi?.enabled` once during
bootstrap, and `resolveWebUiConfig()` runs inside `bootstrapFromEnvironment()`. Enabling the bundled
web UI therefore requires a daemon restart. The web app itself ships with the installed daemon at
`/Applications/Paseo.app/Contents/Resources/app-dist`, so no build or download is needed.

## 5. Ordinary-path baseline with the plugin enabled

Measured through the same supported client while `paseo-knowledge` was enabled and running:

| Ordinary operation | Result |
|---|---|
| `listPlugins` | ok, `2.1 ms` |
| `getProvidersSnapshot` | ok, `3.2 ms` |
| `listAvailableProviders` | ok, 5 providers |
| `fetchAgents` | ok, 6 agents |
| `fetchWorkspaces` | ok, 10 workspaces |
| `getDaemonStatus` | ok, version `0.8.0` |

`listTerminals` returned a schema error caused by the probe passing an object where the daemon
expects a `cwd` string. That is a defect in the probe invocation, not in the daemon or the plugin.

This is read-path evidence only. It does not yet prove ordinary composer/provider/session runtime
behaviour, which needs an actual provider task with the plugin enabled and again disabled.

## 6. Criterion status after this pass

| Criterion | Before | Now |
|---|---|---|
| Supported client-to-daemon transport | OPEN, believed broken | **PASS** — `/ws` healthy, diagnosed as hosted-origin limitation |
| Live installed-plugin RPC dispatch | OPEN | **PASS** — five RPCs exercised through `plugin.rpc.invoke` |
| Live typed contract + allowlist enforcement | OPEN | **PASS** — schema rejection and unknown-RPC rejection observed |
| Functional status/search/prepare/context/review over RPC | OPEN | **OPEN, newly diagnosed** — daemon lacks `PASEO_KNOWLEDGE_*` (section 3) |
| Plugin UI panels render and perform typed actions | OPEN | **OPEN** — needs bundled web UI, which needs a restart (section 4) |
| Ordinary composer/provider/session compatibility | OPEN | **PARTIAL** — read paths pass; runtime provider task still required |
| Independent criterion-level audit | OPEN | **OPEN** |

## 7. Test-environment defect found while reproducing the suites

`integrations/paseo-knowledge/package-lock.json` is tracked and pins exactly one rolldown native
binding, `@rolldown/binding-darwin-x64`, so it was generated on x64. This host is `darwin-arm64`.
A strict `npm ci` therefore installs no usable binding and Vitest cannot start at all:

```text
Cannot find module '@rolldown/binding-wasm32-wasi'
Cannot find module './rolldown-binding.wasi.cjs'
```

This also reproduces in the installed worktree, so the previously recorded "plugin Vitest 21 passed"
result is not currently reproducible from the committed lockfile on this machine's architecture.

The lockfile was not regenerated in this pass. `npm install` refuses to re-resolve the platform
variant ("up to date"), and a clean regeneration fails inside npm's own peer resolver
(`TypeError: Cannot read properties of null (reading 'edgesOut')` in `arborist/build-ideal-tree.js`,
npm `10.9.8` / Node `22.23.2`) — an npm bug, not a repository fault. Forcing the issue with
`--legacy-peer-deps` would commit a lockfile resolved under different peer rules, which is worse
than the defect it fixes.

**Recommendation, not applied:** regenerate this lockfile on a machine or CI job where npm can
resolve vitest's peer set, so both `@rolldown/binding-darwin-arm64` and `@rolldown/binding-darwin-x64`
are recorded.

Verification for this pass used the committed lockfile plus the matching `1.2.8` arm64 binding
unpacked into `node_modules` only. `package.json` and `package-lock.json` are unmodified.

## 8. Reproduced verification results

| Suite | Command location | Result |
|---|---|---|
| Repository pytest | repo root | 487 passed, 1 skipped |
| Knowledge unittest discovery | repo root | 66 passed |
| Plugin Vitest | `integrations/paseo-knowledge` | 21 passed, 1 file |
| Plugin TypeScript typecheck | `integrations/paseo-knowledge` | exit 0 |

## 9. Exact next required action

Both remaining blockers are cleared by one authorized daemon restart that reloads the launchd job
definition, optionally combined with enabling the bundled web UI:

1. Back up `~/.paseo/config.json`, then set `features.webUi.enabled = true`.
2. `launchctl bootout gui/501/com.syang.paseo.daemon`
3. `launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.syang.paseo.daemon.plist`
4. Verify: `knowledge.status` reports `configured: true`, and `GET /` serves the web app.

At the time of writing, the only running agent is this acceptance manager; five other agents are
idle. The restart was **not** performed in this pass because restarting the daemon affects other
agents and requires explicit fresh approval.
