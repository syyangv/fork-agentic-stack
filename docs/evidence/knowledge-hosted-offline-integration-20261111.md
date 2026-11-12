# Hosted Knowledge Task Offline Integration — 2026-11-11

## Scope

Base: merged/deployed `05184b74870a15bc57e302a5308a8cd64c87752a`. Branch: `feat/knowledge-hosted-task-integration`. This is an **offline integration seam**, not live hosted execution enablement. No index.server.ts registration, existing KnowledgeTaskService Start change, installed-checkout update, daemon restart, plugin reload, real credential resolution, or external provider request occurs.

## Implemented flow

1. A separately owned hosted disclosure policy pins work scope, no model tools, OpenCode/upstream disclosure, exact HTTPS endpoint, model, version, character/token bounds, freshness and timeout. It is not the offline registry's `network=none` attestation and does not assert OS sandboxing.
2. Trusted preparation/approval code issues a single-use opaque approval ID bound to the exact prompt, work-source manifest, file hashes, excerpt hashes, exclusions, provider/model and policy. Start rejects forged IDs, changed payloads and incompatible policy. Issuing approval is deliberately not a client RPC; production must derive task/workspace authorization from authenticated state before issuing it.
3. The coordinator snapshots and validates context. File `contentHash` is distinct from `excerptHash`; the latter must match the exact text approved for transmission. A required trusted source-check capability verifies current file hashes, before credential acquisition, just before HTTP after credential acquisition, and again before draft capture. Only opaque source IDs, not filesystem paths, are sent to the model.
4. An exclusive, fsync-backed metadata reservation permanently spends the task ID **before** provider submission. Duplicate or interrupted reservations cannot be replayed, including across coordinator/ledger recreation. No context, prompt, output or credentials enter this ledger.
5. The concrete provider adapter invokes the hardened ProviderRunner with the pinned policy and an explicitly injected credential resolver. It resolves credentials for each execution, rejects expired/near-expiry OAuth credentials and never logs key values. There is no default resolver or automatic OAuth refresh.
6. Provider completion ID/model, server execution ID, input digest and output digest bind successful text to its approved request. No Paseo agent/turn IDs are fabricated. Missing/mismatched identities cannot be captured.
7. A concrete private JSON draft sink writes only `pending_review` candidates with source references, never raw source context or prompts. It refuses vault-overlapping/symlink destinations, requires private runtime permissions, uses exclusive synchronous commits/fsync, checks cancellation before commit, and is idempotent for identical executions while rejecting conflicting/corrupt records. It has no formal-note apply capability.
8. The coordinator verifies the draft receipt and persists terminal metadata. Failed persistence or ambiguous capture cannot be reported as completed. Every reservation remains non-replayable.

## Deadline, cancellation and ambiguity

- One absolute deadline bounds asynchronous reservation, source verification, credential/provider work, draft capture and final metadata persistence.
- All operations whose completion is still unknown retain the coordinator's concurrency slot until they settle. Aborted requests cannot be recaptured from late results.
- After provider submission, a local cancellation/timeout cannot prove remote non-execution. It is reported as a non-retryable unknown submission outcome. HTTP/provider errors are also never automatically retried by this coordinator.
- A draft commit that already happened cannot be undone by a later cancellation or storage error; the result becomes incomplete and requires inspection. No automatic recapture or provider replay follows.
- JavaScript deadlines cannot preempt synchronous filesystem syscalls. Private synchronous commits deliberately avoid asynchronous write-after-cancel races; they are not a hard real-time or OS isolation guarantee.

## Verification

All tests use synthetic data, temporary directories, fake credentials, injected transports or local loopback HTTP servers. No real provider/vault/credential access is used.

- Initial coordinator regression run: 3 failed / 95 passed, exposing malformed completion IDs, freshness loss during provider acquisition, and mismatched draft receipts. Fixed and retested.
- Full ARM64 typecheck: PASS.
- Full Vitest: **132 passed / 132 total**, nine test files.
- Concrete end-to-end test: approval -> file hash verification -> exclusive durable reservation -> pinned HTTP adapter/local fake provider -> completion identity -> private pending-review draft -> durable terminal metadata -> restart duplicate refusal. Verifies a changed synthetic file is refused before credential acquisition and a second disclosure.
- Additional tests cover policy denial, forged/changed/reused approvals, source exclusions/hash mismatch, resource bounds, reservation failure, crashes, remote ambiguity, pending-operation concurrency quarantine, cancellation, async storage stalls, stale preparation, credential expiry, corrupt ledger/draft records and private permissions.
- Existing 63 plugin/provider/offline-contract tests remain included.

## Production gates still open

- Integrate with authenticated task-store preparation and an explicit disclosure-confirmation UI. Never expose the registry's approval issuer directly to clients.
- Supply the actual core provenance/source revalidation adapter; the offline tests deliberately use synthetic verifiers. The prepared file hash and excerpt hash must retain their distinct meanings.
- Connect private hosted drafts to the established human-review workflow. This isolated JSON sink is not a migration or replacement of the current draft SQLite database, and it cannot apply a formal note.
- Independently review and authorize production configuration, credentials, provider data handling, and live enablement. Hosted data leaves the Mac; endpoint pinning is not host-wide egress isolation or a guarantee about upstream retention.
- Implement/review OAuth refresh or require explicit renewal on expiry; current behavior fails closed.
- Run a separately authorized synthetic live-provider smoke and physical-iPhone acceptance. Neither is claimed here.

No commit, merge or deployment is performed by this implementation turn. Root-level `node_modules/.vite/vitest/.../results.json` was generated by an initially misplaced draft-sink test run; the test was moved into the integration suite. The cache is left untouched and must not be staged with source changes.

## Independent review and fixes

The first final review found a HIGH power-loss durability gap: a newly created reservation directory entry also needs its parent/ancestor metadata flushed before HTTP submission. Fixed by flushing the reservation marker, leaf directory and every canonical ancestor bottom-up before acknowledging reservation. All metadata-flush failures, including unsupported operations, now fail closed with the task marker left spent. Medium findings addressed: recheck OAuth expiry after asynchronous source authorization, normalize source hashes and reject control-character paths before disclosure, assert terminal completed-ledger identity/digests in the end-to-end test, and inject real sink write/file-fsync/directory-fsync failures. No review finding is treated as permission to enable production execution.

## Final review result

Independent reviewer Huygens approved the offline seam with no remaining actionable HIGH/blocker, conditional on the parent final green suite. Parent then verified ARM64 typecheck and **132/132 tests PASS**. Reviewer explicitly did not approve service enablement or attest OS isolation.

Final evidence: `docs/evidence/logs/knowledge-hosted-offline-integration-20261111/{typecheck,vitest}.txt`. `git diff --check` passes. Existing server entrypoint/Start implementation and deployed revision `05184b7` remain unchanged.
