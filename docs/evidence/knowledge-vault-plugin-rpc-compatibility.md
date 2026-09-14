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
| Current repository source revision | 81f36622b88386717bd5cc9677bee08ede443f31, governed implementation commit |
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

## Acceptance mapping

| Criterion | Status | Evidence and limitation |
|---|---|---|
| Plugin identity/source surface | PASS | Manifest, source path, and provenance above. |
| Typed status/search/context seams | PASS, source/test | Contracts, handler registration, UI useRpc wiring, typecheck, and 21 tests pass. |
| Current installed plugin health/logs | OPEN | After restart, supported status reports deterministic Paseo 0.8 compatibility rejection; earlier 0.7.2 health is historical only. |
| Live work-only status/search/prepare/context RPC | OPEN | No supported in-app host and no live daemon websocket were available; no bypass was attempted. |
| Synthetic core behavior | PASS, bounded | Prior live-handoff smoke recorded status ready, search ok, and build_context ok; this is not live plugin RPC. |
| UI panels render and perform typed actions | OPEN | Source typecheck passes, but the requested in-app UI could not be opened. |
| Ordinary path is not intercepted | PASS, static | Only workspace panels/attachment source are contributed; no global composer interception is present. |
| Ordinary runtime composer/provider/session compatibility | OPEN, overall partial | No live UI/daemon path was available and no real provider task was permitted. |
| Disable/reenable compatibility | OPEN | Not run because active tasks were present and the daemon was unreachable. |
| Unrelated-plugin/global-switch preservation | PASS, scope adherence | No lifecycle/config action was issued by this audit. |

## Remaining safe gate

After the migration worker passes typecheck/tests, reload through supported Paseo mechanisms, confirm current daemon/plugin health, and invoke only synthetic work-scope status/search/prepare/context behavior. Measure ordinary-path compatibility and disable/reenable only after another fresh active-task check. Until then, direct UI/RPC and runtime ordinary-path criteria remain OPEN.
