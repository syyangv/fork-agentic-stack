# Hosted Knowledge Application Candidate — 2026-11-12

## Scope

Continuation on `feat/knowledge-hosted-task-integration`, based on deployed `05184b7`, alongside the uncommitted offline execution-to-draft seam. This adds server-owned preparation, explicit disclosure consent, client recovery controls and private JSON draft review. It is **not deployed or enabled**.

The candidate production entrypoint calls `registerHostedHandlers(server)` **without an application**. All seven hosted RPCs return `hosted_disabled`; the original Start action remains fail-closed. `createCandidateHostedApplication` requires explicit policy, source/store, private runtime and injected credential capabilities and is not called by that entrypoint. It has no automatic environment enable switch or default credential resolver.

## Trust boundary

The pinned Paseo SDK exposes only `context.paseo`, not an authenticated caller/user/workspace principal. The application inherits the existing **single-owner daemon-session** trust boundary. Workspace refresh validates existence/status, not a per-user ACL. No multi-tenant authorization claim or proof of human presence is made. Deployment must separately verify daemon authentication and unauthorized-client rejection; a multi-user requirement needs additional platform support.

## Implemented flow

- `StoredHostedPreparationSource` derives scope, model, source refs and exact excerpt text from the existing server TaskStore. Client execution input cannot contain arbitrary sources/bindings/model/policy. Personal context, bound agents, truncated context, exclusions and changed revisions fail closed.
- `KnowledgeCliRunner.readSource` invokes the existing Python `read_source` API using the original core source ID, line range and expected full-file hash. File hash and excerpt hash remain distinct. Range-specific opaque IDs avoid source-ID collisions across chunks from the same note.
- Revalidation compares authoritative source/vault/note/path/range/hash and exact excerpt bytes. The CLI lookup does not round-trip the search heading label, so that display-only label is intentionally excluded from the reread comparison.
- A server-held preview contains the exact outgoing prompt/excerpts plus provenance, destination/model/disclosure and expiry. Execute accepts only preview/task/workspace IDs, challenge and payload digest. Only explicit consent for that stored snapshot can issue the internal single-use execution approval. Reprepare, expiry, excluded source, modified payload or reused preview cannot authorize execution.
- Preview admission is bounded before asynchronous workspace/CLI work: two global verification slots, one per task, 32 stored previews. Timed-out verification keeps its slot until the actual underlying operation settles.
- Execution intent and its cancellation controller are recorded before the first workspace-refresh await. Cancellation uses the server-held task/workspace binding immediately; it never waits for workspace availability. Pending workspace work cannot later submit after cancellation.
- Aborted CLI calls settle only after the owned child closes. A SIGTERM-ignoring synthetic reader regression verifies that cancellation does not prematurely release resource occupancy.
- Existing offline coordinator/ledger/provider/draft boundary remains in use. No Paseo agent or turn is fabricated. The application does not invoke a formal-note writer.
- Private draft review supports bounded list/read and digest-bound rejection only. Rejection writes a durable separate marker; the original draft and formal notes remain unchanged. No apply/delete RPC exists. This JSON review flow is separate from, and does not migrate, the existing SQLite draft store.

## Client behavior

The candidate workspace panel mounts the separate hosted component. Work-workspace-only preparation controls are gated; disabling new preparation does not hide cancellation of an already submitted snapshot. Exact prompt and all source excerpts are shown before an initially unchecked consent control can enable execution.

Expiry, scope/identity/revision edits and stale asynchronous results invalidate consent. A transport failure retains the original cancellation identity as an unknown outcome, permits read-only draft recovery and never retries automatically. Explicit recovery acknowledgement does not authorize retrying the same task. Cancellation continues to target the original task/workspace after prop edits. Draft output is untrusted plain Text, not HTML, executable code or a URL action. Rejection requires a second explicit confirmation.

## Verification

- ARM64 typecheck: **PASS**.
- Full Vitest: **196/196 PASS**, 16 test files.
- `git diff --check`: PASS.
- Real Python core test uses an empty synthetic HOME, synthetic work/personal/runtime directories and a fake local HTTP provider. It exercises index/prepare -> range/hash revalidation -> disclosure -> explicit consent RPC -> pinned adapter -> private draft list/read/reject. It asserts one provider request, no paths sent to the provider, null agent/turn IDs and byte-identical formal note content after review.
- Actual React test-renderer tests cover transport loss after prop changes, retained original cancellation, read-only recovery/no retries, normal completion, A-to-B-to-A stale preview protection, mode gating and rejection confirmation. These are component tests, **not native visual or physical-iPhone verification**.
- New dev-only dependencies: `react-test-renderer@19.1.0` and matching types, using the existing React version. Three packages added; only 35 lock/manifest lines changed. Actual lifecycle coverage was necessary because helper-state tests missed an unconditional cleanup bug in the component.
- Npm audit reports seven advisories: two moderate, four high, one critical. Every affected version is unchanged from the base lockfile. No renderer advisory was introduced. No broad `npm audit fix` or dependency upgrade was applied. See the audit summary for package-level baseline comparison; production exploitability was not assessed.

Logs: `docs/evidence/logs/knowledge-hosted-application-20261112/`.

## Review

Interim review found cancellation before workspace refresh and premature client/child-process cleanup gaps; regressions and fixes are included above. Final independent rereview approved the offline/default-disabled candidate with no remaining HIGH/blocker. It verified cancellation before workspace refresh, transport-loss recovery, child-close quarantine, regression coverage and default-disabled registration. Reviewer inspected the tests; parent ran typecheck and all 196 tests successfully. This is not production enablement approval.

## Remaining production gates

1. Explicitly review and authorize the concrete production assembly, daemon auth policy, disclosure/provider terms, credentials and dependency advisories. No hosted application is instantiated by default.
2. Complete a native/browser visual pass and physical-iPhone acceptance, including consent expiry, close/reopen/reconnect and unknown-outcome recovery. Component tests are not a substitute.
3. Obtain separately authorized synthetic live-provider validation. No real key export, OAuth refresh, provider request or vault disclosure was performed in this turn.
4. Only after review and approval: commit/push/merge/deploy the intended revisions and opt in explicitly. The deployed checkout remains `05184b7` with hosted execution disabled.

The earlier repo-root Vitest cache under `node_modules/.vite/` remains untouched and is not source to stage.
