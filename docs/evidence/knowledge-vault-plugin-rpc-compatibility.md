# Knowledge Vault Plugin RPC/UI Compatibility Evidence

Date: 2026-09-14
Worktree: /Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault
Scope: read-only audit; the only repository write by this audit is this evidence file.

## Scope and safety boundary

- No implementation, test, plan, tracker, daemon/provider/config, or unrelated plugin files were changed.
- No personal vault, personal source, credential, browser cookie/session store, real provider task, or real user task was read or invoked.
- No internal or unsupported RPC bypass, direct IPC, network endpoint, Chrome substitution, or global plugin switch change was used.
- Temporary plugin disable/reenable was not attempted: the active-task check found running work in this worktree, and the daemon CLI was not reachable.

## Plugin identity and provenance

| Item | Evidence |
|---|---|
| Plugin ID | paseo-knowledge, from integrations/paseo-knowledge/paseo-plugin.json |
| Source | /Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault/integrations/paseo-knowledge |
| Current repository source revision | `97a5c53dcc40009569fc274b088b0420f67061fc`, Paseo 0.8 migration commit; merged through PR #31 as `9e442f01f8dd96a2c67bfcc81ca92b368213d8b4` |
| Prior installation record | live-handoff evidence records installation from this source and earlier enabled=true, status=running; this audit could not independently refresh that historical state |
| Prior implementation provenance | 449957125718e822020ad664d59a77b2a975df61, merged as a742258d923b920252057f4a0bf194df74f74098 |

## Supported runtime/UI attempts

- The supported in-app Paseo browser host returned: Browser not available: iab.
- Browser troubleshooting showed only a Chrome extension host. It was not substituted because the requested in-app host was unavailable.
- Earlier live handoff evidence separately records a Paseo browser automation timeout.
- At approximately 2026-09-14 11:31:39 EDT, supported plugin list/status/log calls failed with transport closed (code 1006).
- At approximately 11:32:19 EDT, supported status reported localDaemon=unresponsive, connectedDaemon=unreachable, PID 63079, and websocket 127.0.0.1:6767 unreachable.
- The supported agent-list CLI likewise returned DAEMON_NOT_RUNNING / transport closed (1006). No bypass or direct IPC was attempted.

## Active-task safety check

Before any possible lifecycle action, the supported Paseo agent surface found these running tasks:

| Short ID | Status | Role |
|---|---|---|
| fdff532 | running | Plugin RPC compatibility audit |
| 9cacf24 | running | Five synthetic E2E and latency |
| 596e7a6 | running | Knowledge Vault implementation manager |

Result: do not disable or reenable paseo-knowledge while active work is present. No lifecycle command was issued; unrelated plugins and global configuration were preserved.

## Static plugin surface and test evidence

- Typed contracts include knowledge.status, knowledge.search, knowledge.task.prepare, knowledge.task.start, reconciliation, source removal, and attachment search/source.
- Handlers are registered through plugin.handle; the attachment source is registered through plugin.addAttachmentSource.
- Only knowledge workspace and knowledge agent panels are contributed; panels use Paseo workspace/agent hooks and typed useRpc calls.
- Provider/model is caller-selected; work is the default scope and personal scope displays a non-persistence warning.
- Static no-formal-write assertions pass; no client-side command-center interception, review_apply, or formal-write contribution is present.
- Paseo TypeScript typecheck passed.
- Correct repository-root Vitest invocation passed: 1 file, 21 tests, exit 0. The package-local npm test invocation is a working-directory/config discovery mismatch, not a test failure.
- These checks use synthetic payloads and fake SDK objects; they do not prove live installed-plugin RPC dispatch.

## Post-restart manager reconciliation

At 2026-09-14 18:00:35 EDT, the authorized supported restart completed and the daemon reported version 0.8.0. The installed configured plugin then reported enabled=true and status=failed with this deterministic startup error:

    Plugin paseo-knowledge requires Paseo <0.8.0. Your daemon is 0.8.0. This plugin has no requirements.paseo and targets Paseo before 0.8.

This supersedes the earlier transport-only observation as the current startup blocker. The source uses the pre-0.8 root entry shape and must be migrated to the documented 0.8 runtime entries before reload. No plugin RPC was claimed during this failed state, and no personal or provider data was accessed.

## Manager follow-up after migration — 2026-09-14

The scoped migration changed the plugin to Paseo 0.8 runtime entries, added requirements.paseo >=0.8.0, moved client/server/shared modules to their runtime directories, and pinned the development SDKs to 0.8.0. Final-source checks passed: npm run typecheck and repository-root Vitest, 1 file / 21 tests.

At 2026-09-14 14:18 EDT, supported paseo plugin reload paseo-knowledge returned enabled=true and status=running. The daemon log recorded Loading plugin followed by Plugin ready at 2026-09-14 18:18:47Z (14:18:47 EDT). Subsequent supported plugin status and logs also returned running and the expected ready entry. The prior 0.8 manifest rejection is therefore resolved for the current installed source path.

Controlled lifecycle compatibility was then exercised one command at a time after a fresh active-task check found no other worker in this worktree:

1. paseo --json plugin disable paseo-knowledge returned enabled=false, status=disabled.
2. paseo --json plugin ls confirmed the disabled state.
3. paseo --json plugin enable paseo-knowledge returned enabled=true, status=running.
4. paseo --json plugin status and logs confirmed running and Plugin ready at 14:20:23 EDT.

No other plugin was configured, no daemon-wide switch was changed, and no real provider/composer task was run. This proves scoped plugin teardown/restart behavior, not runtime ordinary-path compatibility. Direct installed plugin UI/RPC remains open because the supported browser host exposes daemon health and a blank root (web UI is not enabled), but no plugin panel/RPC client invocation path was available.

## Acceptance mapping

| Criterion | Status | Evidence and limitation |
|---|---|---|
| Plugin identity/source surface | PASS | Manifest, source path, and provenance above. |
| Typed status/search/context seams | PASS, source/test | Contracts, handler registration, UI useRpc wiring, typecheck, and 21 tests pass. |
| Current installed plugin health/logs | PASS after migration | Supported status reports enabled=true, status=running; logs report Loading plugin and Plugin ready on Paseo 0.8.0. The earlier compatibility rejection is historical. |
| Live work-only status/search/prepare/context RPC | OPEN | No supported in-app host and no live daemon websocket were available; no bypass was attempted. |
| Synthetic core behavior | PASS, bounded | Prior live-handoff smoke recorded status ready, search ok, and build_context ok; this is not live plugin RPC. |
| UI panels render and perform typed actions | OPEN | Source typecheck passes, but the requested in-app UI could not be opened. |
| Ordinary path is not intercepted | PASS, static | Only workspace panels/attachment source are contributed; no global composer interception is present. |
| Ordinary runtime composer/provider/session compatibility | OPEN, overall partial | No live UI/daemon path was available and no real provider task was permitted. |
| Disable/reenable compatibility | PASS, scoped lifecycle | One-at-a-time disable, confirmed disabled state, enable, and ready-state verification passed after a fresh active-task check. This does not substitute for an ordinary provider/composer runtime task. |
| Unrelated-plugin/global-switch preservation | PASS, scope adherence | No lifecycle/config action was issued by this audit. |

## Remaining safe gate

The migration, reload, current health, and scoped lifecycle checks now pass. The remaining safe gate is direct installed-plugin UI/RPC invocation through a supported Paseo client, followed by runtime ordinary-path compatibility evidence; use only synthetic work-scope data and a fresh active-task check before any further lifecycle action. Until then, direct UI/RPC and runtime ordinary-path criteria remain OPEN.

## Final delivery reconciliation — 2026-09-19

PR #32 merged the final live-verification documentation as ae774a8a10bf857f3637b68ddeb7f160e6d1259f; the installed source branch remains 3f3f515996afbbbaa0792819a3a10d64b1d1ba38. On daemon 0.8.0, supported plugin ls/status reported enabled=true and status=running. A later standalone plugin logs call returned transport closed (code 1006), so log retrieval is intermittent; this does not change the successful reload/status/lifecycle evidence, but direct UI/RPC remains OPEN.
