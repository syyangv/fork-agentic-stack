# Agent Knowledge Vault — Independent Adversarial Acceptance Audit

Date: 2026-09-19
Repository under review: d74e9d7156a8adfc4b157fcde17b1cda25345c2c (PR #44 merge)

## Boundary and method

This is an independent, read-only audit. I did not read the personal vault
/Users/syang/obsidian/syang, access credentials, spawn subagents, change daemon/config/plugin
lifecycle state, make network calls, or modify any file other than this report. The scoped work
vault and runtime paths were treated as read-only evidence locations.

The code-review graph had no nodes for the plugin symbols, so it was treated as stale and direct
source inspection was used instead. The plan phase criteria are in
docs/plans/2026-09-13-agent-knowledge-vault-implementation.md:195-303; its scenario matrix is at
:307-326.

## Independently rerun headline counts

| Check | Actual observation |
|---|---|
| PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider | 487 passed, 1 skipped in 25.72s, exit 0 |
| PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_knowledge*.py' -q | Ran 66 tests in 1.499s; OK, exit 0 |
| (cd integrations/paseo-knowledge && npm run typecheck) | exit 0 |
| ./integrations/paseo-knowledge/node_modules/.bin/vitest run --config integrations/paseo-knowledge/vitest.config.mjs --configLoader runner --reporter=dot | exit 1; no test summary; Cannot find module @rolldown/binding-wasm32-wasi and Cannot find module ./rolldown-binding.wasi.cjs |
| /Users/syang/.local/bin/paseo --json plugin ls (read-only health check) | exit 1; observed Transport closed |

The plugin Vitest result is not currently reproducible from the checked-in dependency state on
this host. No install, lockfile change, or workaround was performed.

## Phase verdicts

The phase verdict is deliberately the strictest state of the criteria underneath it.

| Phase / criterion | Verdict | Evidence and limitation |
|---|---|---|
| P0 compatibility and frozen contracts | PARTIAL | Static contracts and synthetic seams exist, and live dispatch is evidenced; live UI/task creation/turn/timeline/reopen are not proven. live-rpc-connectivity-20260919.md:39-61,136-146. |
| P1 vault and authorization boundary | PASS-SYNTHETIC | Bootstrap, allowlists, canonical-path checks, task-scoped grants, and personal quarantine are fixture/test-only proof. task-store.ts:197-300; plugin tests:198-226,472-488,602-639. |
| P2 parser and rebuildable index | PASS-SYNTHETIC | Core tests and synthetic fixtures prove parser/index behavior; no real vault was used. p7-synthetic-evaluation.md:8-29,90-95. |
| P3 retrieval, provenance, and context | PARTIAL | Retrieval/hash/bounds/gating are synthetic; P3.8/T09 is not technically isolated from prompt injection. service.ts:625-642 only adds prompt text and labels. |
| P4 Paseo thin plugin | PARTIAL | Static surface, input contract validation, allowlist rejection, and live handler dispatch pass; functional task flow is blocked by configured:false, UI is disabled, and ordinary provider runtime was not run. live-rpc-connectivity-20260919.md:63-145. |
| P5 capture and drafts | PASS-SYNTHETIC | Candidate-only capture, completion gating, idempotency, durable metadata, and personal quarantine pass only at fake/installed Python-core boundaries. live-e2e.md:44-136. |
| P6 human approval and recovery | PASS-SYNTHETIC | Core approval/hash/journal/retrieval evidence is synthetic or Python/core; the plugin exposes no live approval UI. live-e2e.md:60-82,178-245; plugin tests:650-674. |
| P7 evaluation and controlled rollout | OPEN | P7.2 and bounded synthetic tasks pass, but P7.3/P7.4 live task/UI/ordinary-path gates, P7.5 events, and the authorized restart/UI gate remain open. live-handoff.md:9,149-162. |

## Criterion-by-criterion verdicts

### P0

| Criterion | Verdict | Evidence |
|---|---|---|
| P0.1 installed runtime/plugin/version/source difference without auth read | PASS | Daemon 0.8.0, server, installed source, and no-credential boundary: live-rpc-connectivity-20260919.md:3-6; migration/health evidence: plugin-rpc-compatibility.md:64-77. |
| P0.2 synthetic panel/RPC/borrowed connection/agent/turn/timeline | PARTIAL | Live RPC dispatch is proven for status/search/attachment and input rejection, but the probe never creates an agent or reads a turn/timeline: live-rpc-connectivity-20260919.md:39-61; live-rpc-probe.mjs:42-65. |
| P0.3 close/reopen panel and recover by agent/turn ID | OPEN | Direct UI/RPC remains open: plugin-rpc-compatibility.md:77-96; no live panel was rendered. |
| P0.4 absolute Python executable and FTS5 capability | PASS-SYNTHETIC | P0 records Python/SQLite/FTS5 checks: p0.md:30-40; runner rejects non-absolute executable/CLI paths: server/core.ts:73-103,232-235. |
| P0.5 new vault/private runtime paths and repository exclusion | PASS-SYNTHETIC | Initialization created only synthetic work vault/runtime and skipped existing files: live-handoff.md:15-30; git status showed only pre-existing untracked integrations/paseo-knowledge/node_modules/. |
| P0.6 SourceRef/ContextBundle/TaskRecord/KnowledgeDraft fixtures | PASS-SYNTHETIC | Frozen contract fixtures and bounded provenance: p0.md:134-159; RPC schemas: shared/contracts.ts:41-97. |

### P1

| Criterion | Verdict | Evidence |
|---|---|---|
| P1.1 bootstrap Home/directories/templates without overwrite | PASS-SYNTHETIC | live-handoff.md:15-30 records created paths and skipped 0 files. |
| P1.2 work allowlist; personal identity only/no index | PASS-SYNTHETIC | P0 boundary says personal access is not indexed by default: p0.md:116-130; work-only durable source filtering: task-store.ts:664-669,737-753. |
| P1.3 canonical path, traversal, cross-vault, symlink checks | PASS-SYNTHETIC | PersonalGrantStore checks lexical and real paths: task-store.ts:279-300; symlink test: paseo-knowledge.test.ts:216-226. |
| P1.4 stable IDs and move/delete semantics | PASS-SYNTHETIC | Core ID/move/duplicate tests: test_knowledge_scope.py:180-207; test_knowledge_index.py:49-123. |
| P1.5 task-scoped personal grant without scope expansion | PASS-SYNTHETIC | In-memory task map, expiry, relative/canonical path checks: task-store.ts:197-263,269-300; grant/revoke tests: paseo-knowledge.test.ts:198-226. |
| P1.6 link-only, no mirroring content rule | PASS-SYNTHETIC | Runtime state excludes context, session text, personal excerpts and grant paths: README.md:40-47; persistence test: paseo-knowledge.test.ts:602-639. |

### P2

| Criterion | Verdict | Evidence |
|---|---|---|
| P2.1 conservative Markdown block parser | PASS-SYNTHETIC | Parser tests cover frontmatter, headings, fences and links: tests/test_knowledge_parser.py; fixture corpus: p7-synthetic-evaluation.md:8-29. |
| P2.2 bounded heading/paragraph chunking | PASS-SYNTHETIC | Fixed synthetic evaluation and bounded context results: p7-synthetic-evaluation.md:31-64; live-e2e.md:44-59. |
| P2.3 SQLite source/chunk/link plus parameterized FTS5 | PASS-SYNTHETIC | Rebuildable SQLite/FTS5 behavior is covered by 66 knowledge tests and 20-query fixtures: p7-synthetic-evaluation.md:29-64. |
| P2.4 add/modify/delete/move detection without resident watcher | PASS-SYNTHETIC | Move/delete/immutability tests: test_knowledge_index.py:49-93; no live watcher or real vault used. |
| P2.5 transactional refresh/rebuild without Markdown mutation | PASS-SYNTHETIC | Index tests cover rebuild/incremental/immutability: test_knowledge_index.py:49-151; rerun suite was 487/1. |
| P2.6 multilingual/empty/large/invalid/symlink/duplicate-ID fixtures | PASS-SYNTHETIC | Scope/index/parser modules and duplicate-ID assertions: test_knowledge_index.py:93-151; test_knowledge_scope.py:51-180. |

### P3

| Criterion | Verdict | Evidence |
|---|---|---|
| P3.1 title/alias/body matching with explicit project filter | PASS-SYNTHETIC | Fixed 20-query oracle and project-filter Q08/Q09: p7-synthetic-evaluation.md:31-56. |
| P3.2 deterministic ranking/tie ordering | PASS-SYNTHETIC | Fixed query set and repeatable result paths: p7-synthetic-evaluation.md:31-64. |
| P3.3 safe Chinese trigram/short literal handling | PASS-SYNTHETIC | FTS5 limitation/fallback: p0.md:30-40; Chinese/short-literal rows Q03/Q10/Q14/Q19/Q20: p7-synthetic-evaluation.md:37-56. |
| P3.4 merged snippets, 8-snippet/12,000-char bound and truncation | PASS-SYNTHETIC | Explicit CLI limits: live-e2e.md:193-197; bounded measurements: live-e2e.md:145-169. |
| P3.5 explicit related links only, no recursive graph read | PASS-SYNTHETIC | includeRelated is explicit in the contract: shared/contracts.ts:116-125; context tests use bounded source selection. |
| P3.6 hash-check before ContextBundle | PASS-SYNTHETIC | Stale fixture returned empty context and old hash, not stale text: live-e2e.md:119-136. |
| P3.7 personal grant only on-demand, no persistent index, provenance flag | PASS-SYNTHETIC | Service requires task grant and passes paths only to supervised seam: service.ts:152-185,194-223; current CLI refuses personal transport: core.ts:111-131. |
| P3.8 source cannot expand privileges/trigger shell/approval | OVERSTATED | Implementation only places a warning and untrusted text in one agent prompt: service.ts:625-642; sourceContentUntrusted is a flag, not a tool/policy guard: contracts.ts:126-130. No malicious-source/tool/approval test or live task proves isolation. |

### P4

| Criterion | Verdict | Evidence |
|---|---|---|
| P4.1 client/server/shared split; server calls CLI; client does not read vault | PASS-SYNTHETIC | Entrypoints register server handlers vs client panels: index.server.ts:16-31, index.client.tsx:5-24; CLI boundary: core.ts:68-73. |
| P4.2 task/workspace/provider/context/omit-source/personal-grant UI | PASS-SYNTHETIC | Panel contains typed controls/actions: client/panel.tsx:112-158; UI rendering/action remains unverified live. |
| P4.3 SDK agent creation and caller-selected canonical provider | PASS-SYNTHETIC | Provider schema requires a slash and is caller-selected: contracts.ts:4-8,154-167; create path uses provider: service.ts:245-260. |
| P4.4 stable task ID bound to agent/turn with source/output contract | PARTIAL | Code binds task/agent/turn and sends a contract prompt: service.ts:251-280,625-642; no live task flow. |
| P4.5 attachment source for ordinary composer, no global send interception | PASS-SYNTHETIC | Attachment source and panels are the only client contributions: index.client.tsx:5-24; static no-interception test: paseo-knowledge.test.ts:650-674. |
| P4.6 lifecycle/timeline updates and reopen reconciliation | PASS-SYNTHETIC | Lifecycle subscriptions/reconciliation exist: service.ts:266-269,291-397; UI/live reopen remains unproven. |
| P4.7 unknown create result retained and not automatically retried | PASS-SYNTHETIC | Source returns agent_create_unknown and says no retry: service.ts:261-264; synthetic SDK tests cover identity behavior. |
| P4.8 visible retrieval/provider/approval/timeout/source errors and retained identity | PASS-SYNTHETIC | Error contracts: contracts.ts:14-37; wait timeout retains identity: service.ts:400-443. |
| Live installed-plugin RPC dispatch | PASS | Supported client probe reports five dispatch attempts, typed responses/rejections, and enabled/running plugin: live-rpc-connectivity-20260919.md:39-61; probe calls invokePluginRpc: live-rpc-probe.mjs:48-65. This proves dispatch, not functional task flow. |
| Live input contract validation and contributed-RPC allowlist | PASS | Missing workspaceId/provider and bogus method were rejected live: live-rpc-connectivity-20260919.md:52-58. |
| Functional status/search/prepare/context/review over live RPC | OPEN | Live status was configured:false with the absolute-path environment error: live-rpc-connectivity-20260919.md:63-97. Restart needed to reload stale launchd environment was not authorized. |
| Plugin UI panels render and perform actions | OPEN | Bundled UI disabled and not reloadable; enabling requires restart: live-rpc-connectivity-20260919.md:99-115,136-146. |
| Ordinary composer/provider/session compatibility | OPEN | Only read paths passed; no provider task proves runtime behavior: live-rpc-connectivity-20260919.md:117-145. |

### P5

| Criterion | Verdict | Evidence |
|---|---|---|
| P5.1 structured candidate output contract | PASS-SYNTHETIC | Agent prompt names summary/candidate/source/validation/uncertainty fields: service.ts:625-642; capture envelopes: live-e2e.md:60-80. |
| P5.2 schema/size/source validation and capture_incomplete | PASS-SYNTHETIC | Core capture evidence records malformed/failed-turn quarantine: live-e2e.md:119-136; knowledge.py:218-283. |
| P5.3 task+turn+candidate-hash idempotency | PASS-SYNTHETIC | Duplicate capture remains one row/artifact and same hash: live-e2e.md:101-117. |
| P5.4 personal context not durable without explicit authorization | PASS-SYNTHETIC | Task store clears personal context and persistence test asserts no excerpt/path: task-store.ts:373-393,664-669; paseo-knowledge.test.ts:472-488,602-639. |
| P5.5 ordinary candidates go to Inbox; records are bounded metadata | PASS-SYNTHETIC | Draft/approval evidence separates Inbox draft from formal target: live-e2e.md:60-82; README.md:40-47. |
| P5.6 agent-reported vs observable evidence | PASS-SYNTHETIC | P0 fixture uses agent_reported and rejects unverified claims: p0.md:149-156; service requires verified completion: service.ts:516-579. |
| P5.7 reopen/reconcile last turn without guessing | PASS-SYNTHETIC | Durable records omit context/session/grants and retain identities: task-store.ts:108-166; reconciliation refuses mismatch/no verified completion: service.ts:291-397. No live panel reopen. |

### P6

| Criterion | Verdict | Evidence |
|---|---|---|
| P6.1 review panel shows candidate/source/level/path/diff | OPEN | Client has a task panel, not approval/diff panel; no review_apply or formal-write contribution: client/panel.tsx:112-184; paseo-knowledge.test.ts:650-674. |
| P6.2 accept/edit-accept/reject with terminal rejection | PASS-SYNTHETIC | Python/core approval commands require explicit human approval/rejection: knowledge.py:334-424; synthetic approval tests passed in the 66-test run. |
| P6.3 target existence/hash and reapproval after edit | PASS-SYNTHETIC | Approval target/hash checks and conflicts: test_knowledge_approval.py:1-291; e2e desired/post-write hashes equal: live-e2e.md:74-82. |
| P6.4 atomic/private/serialized/rechecked write | PASS-SYNTHETIC | Approval implementation/tests and journal evidence: live-e2e.md:74-82,178-214; no plugin formal writer. |
| P6.5 hash does not claim to remove all external-write races | PASS-SYNTHETIC | Approval conflict/hash tests; README retains boundary and no formal writer: README.md:35-47. |
| P6.6 idempotent apply journal and recovery | PASS-SYNTHETIC | Approval/recovery scenarios and apply journal commands: live-e2e.md:74-82,205-214. |
| P6.7 accepted note refreshes only affected index and retains provenance | PASS-SYNTHETIC | E2E reports index_updated=true, formal target, and one audit row: live-e2e.md:74-82. |
| P6.8 only add/single-file modify; no automatic move/delete/merge | PASS-SYNTHETIC | Plugin invokes propose_note only and has no review/apply RPC: core.ts:153-179; static test: paseo-knowledge.test.ts:650-674. |

### P7

| Criterion | Verdict | Evidence |
|---|---|---|
| P7.1 synthetic fixture suite, no real private content | PASS-SYNTHETIC | Scope excludes real/personal/daemon data: p7-synthetic-evaluation.md:3-10; core rerun 66 and repository rerun 487/1. Plugin Vitest did not run. |
| P7.2 fixed 20-query mixed-language evaluation | PASS-SYNTHETIC | 20/20 expected-source hits, 22/22 source locations, 100% on fixtures: p7-synthetic-evaluation.md:31-64. |
| P7.3 five real end-to-end tasks | PASS-SYNTHETIC | Five bounded Tasks A-E pass only at installed Python/core synthetic boundary; evidence says no real provider and live RPC remains open: live-e2e.md:23-40,135-136,236-245. |
| P7.4 personal grant/restart/offline/archive/duplicate/recovery coverage | PARTIAL | Grant, duplicate, stale, and durable-recovery cases are synthetic; live client offline/archive/plugin-restart task flow was not exercised. live-e2e.md:101-136; paseo-knowledge.test.ts:198-226,602-639. |
| P7.5 data-layer events with stable task/turn IDs and no prompt/body metrics | OPEN | No artifact proves required data-layer event writes or metric exclusion; current evidence emphasizes bounded runtime state rather than event integration. |
| P7.6 install/disable/rebuild/recovery/grant/review documentation and no global interception | PASS-SYNTHETIC | README documents runtime boundaries and no formal-write/global composer path: README.md:6-47; static test: paseo-knowledge.test.ts:650-674. |
| P7.7 authorized controlled enablement | OPEN | Required restart/config/UI enablement explicitly unperformed: live-rpc-connectivity-20260919.md:185-197; no restart attempted in this audit. |

## Scenario matrix verdicts

| Scenario | Verdict | Evidence and limitation |
|---|---|---|
| T01 unauthorized personal query/traversal/symlink | PASS-SYNTHETIC | PersonalGrantStore rejects absolute/traversal/outside-realpath paths: task-store.ts:207-300; symlink test: paseo-knowledge.test.ts:216-226. |
| T02 one-file personal grant, task-only and non-persistent | PASS-SYNTHETIC | Grant expiry/task map and no-persistence tests: task-store.ts:197-263; paseo-knowledge.test.ts:472-488,602-639. |
| T03 journal/attachment/draft/archive excluded by default | PASS-SYNTHETIC | Formal corpus restricted to four directories and excludes Inbox drafts: p7-synthetic-evaluation.md:8-10; live-e2e.md:145-148. |
| T04 Chinese short/3+ chars/mixed quotes without arbitrary FTS expression | PASS-SYNTHETIC | FTS limitation/fallback and mixed/short query rows: p0.md:30-40; p7-synthetic-evaluation.md:37-56. |
| T05 duplicate note ID | PASS-SYNTHETIC | Duplicate-ID error tests: test_knowledge_index.py:93-151. |
| T06 discard/rebuild index without Markdown change | PASS-SYNTHETIC | Rebuild/incremental/immutability tests: test_knowledge_index.py:49-93,140-151. |
| T07 source changed after search before send | PASS-SYNTHETIC | Stale context emptied and old hash reported; failed turn quarantined: live-e2e.md:119-136. |
| T08 8 snippets/12,000 chars | PASS-SYNTHETIC | Explicit CLI limits and bounded context: live-e2e.md:193-197; p7-synthetic-evaluation.md:31-64. |
| T09 prompt injection cannot expand tools/trigger approval | OVERSTATED | Only advisory labels/prompt text exist: service.ts:625-642; no tool policy, malicious fixture, or live provider test proves isolation. |
| T10 missing completion marker/timeout | PASS-SYNTHETIC | Wait preserves task identity and reports timeout/unknown: service.ts:400-443; reconciliation requires verified completion: service.ts:370-397. |
| T11 duplicate completion/apply | PASS-SYNTHETIC | In-flight capture key and duplicate artifact/hash evidence: service.ts:516-531; live-e2e.md:101-117. |
| T12 source changed during approval | PASS-SYNTHETIC | Approval hash/conflict tests and equal post-write hash: test_knowledge_approval.py; live-e2e.md:74-82. |
| T13 apply complete before state update interruption | PASS-SYNTHETIC | Journal/recovery tests and recorded apply journal commands: live-e2e.md:205-214. |
| T14 rejected draft receives same result again | PASS-SYNTHETIC | Draft-store terminal/replay tests: test_knowledge_capture.py and approval suite; no live plugin flow. |
| T15 personal material auto-capture | PASS-SYNTHETIC | Personal context cleared from state and capture carries provenance flag: paseo-knowledge.test.ts:472-488,602-639; no real personal material. |
| T16 client close/reopen/archive | PARTIAL | Durable identity/reconcile logic tested synthetically: task-store.ts:108-166; no live client close/reopen/archive path. |
| T17 disabled plugin leaves ordinary composer/provider/session unaffected | OPEN | Read-only ordinary paths passed, but no provider task/composer/session runtime: live-rpc-connectivity-20260919.md:117-145. |
| T18 approved Task A retrievable by Task B with provenance | PASS-SYNTHETIC | Separate-task approved reuse and formal source paths: live-e2e.md:84-100. |

## Security review of plugin RPC surface

| Finding | Severity | Independent finding |
|---|---|---|
| Untrusted source is prompt-injected into a tool-capable agent | HIGH | buildTaskMessage concatenates bounded context into the same task message and relies on English instructions/UNTRUSTED SOURCE DATA; agent creation sets provider/title/labels but no source-taint/tool-deny policy (service.ts:254-270,625-642). A malicious indexed note could attempt shell, file, or approval actions. P3.8/T09 is not proven. |
| Personal grant/revoke has no visible caller or task-owner authorization | MEDIUM | RPC handlers accept arbitrary client-supplied taskId and path (index.server.ts:26-29, contracts.ts:210-232, service.ts:450-468). Grants are bounded, expiring, in-memory, and current CLI refuses personal reads (core.ts:111-131), limiting immediate exfiltration; a future personal core would raise severity. |
| Grant path check has a check/use race for a future personal reader | MEDIUM | safeCanonicalRelative realpaths a path and returns a relative string (task-store.ts:287-300); pathsFor rechecks but returns a path for a later read (:244-263). No descriptor/open-under-root operation or lock spans validation/read. Current personal retrieval is unavailable, so this is latent. |
| Output contract is shallow for core payloads | MEDIUM | CorePayloadSchema is z.record(z.string(), z.unknown()) and search/context outputs accept it (shared/contracts.ts:53,126-150). Runtime source-ref filtering is shape-based (task-store.ts:373-381,811-823), while durable filtering is stricter. |
| Automatic formal write path | PASS | Plugin invokes only propose_note into private draft store (core.ts:153-179); no review_apply, formal-write RPC, or global composer interception is contributed (paseo-knowledge.test.ts:650-674). This is source/static proof, not live UI proof. |

## Evidence overstatement and contradiction register

1. OVERSTATED — plugin Vitest result. live-rpc-connectivity-20260919.md:176-183 calls the plugin suite 21 passed, while its own :148-174 says the committed lockfile cannot start Vitest on this architecture. Independent rerun exited 1 with the two rolldown module errors above. plugin-rpc-compatibility.md:45-54,64-68 repeats the historical 21-tests-pass claim without current reproducibility qualification.
2. OVERSTATED — attachment RPC is live and answering. The live table treats knowledge.attachments.search returning items:[] as an answering attachment source (live-rpc-connectivity-20260919.md:48-56). The same run reports configured:false (:63-72), and service.ts:471-474 converts any search error into an empty items list. This proves dispatch/response shaping, not successful attachment search.
3. OVERSTATED/stale — daemon environment propagation. live-handoff.md:97-107,151-158 says six variables were propagated and verifies the installed core boundary. The newer live diagnosis records that the loaded daemon environment has no PASEO_KNOWLEDGE_* values and returns configured:false (live-rpc-connectivity-20260919.md:63-97). The newer artifact supersedes the earlier claim for current acceptance.
4. Stale contradictory status — old plugin RPC/UI evidence. plugin-rpc-compatibility.md:84-91 still labels live work-only RPC/UI open without incorporating newer live dispatch proof, while live-rpc-connectivity-20260919.md:39-61,136-146 correctly splits dispatch PASS from functional flow OPEN. This is an underclaim, not evidence of full acceptance.
5. Stale counts — not current headline counts. live-handoff.md:151-153 reports 451 passed, 1 skipped, and p7-synthetic-evaluation.md:74-80 reports 62 tests; current reruns are 487/1 and 66. These old counts must not be presented as current repository totals.

No evidence document reviewed claims full P0–P7 acceptance. The contrary limitation is explicit in
live-handoff.md:9, live-e2e.md:236-245, and live-rpc-connectivity-20260919.md:136-146. P7 remains OPEN.

## Final verdict counts

The exact counts below are generated from the verdict column after writing this report.

| Verdict | Count |
|---|---:|
| PASS | 3 |
| PASS-SYNTHETIC | 66 |
| PARTIAL | 7 |
| OPEN | 9 |
| OVERSTATED | 2 |

The counts exclude the security severity table.
