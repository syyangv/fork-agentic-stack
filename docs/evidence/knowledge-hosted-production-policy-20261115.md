# Hosted production-policy candidate — 2026-11-15

## Scope

Branch `feat/knowledge-hosted-production-policy`, based on deployed `f0796fa`. Implemented directly; one later managed reviewer independently inspected the deployment candidate and was archived on completion. At the implementation checkpoint no live operator file, credential resolution, model call, reload/restart, Git delivery, deployment or signed-app modification had occurred. The owner subsequently authorized independent review, commit/push and default-disabled deployment; actual delivery outcome is recorded in the recovery handoff.

This closes the missing **code assembly** path, not production authorization or acceptance. Absent/disabled/invalid configuration still yields no hosted application. The disabled example cannot enable credential reuse or pass its expired/placeholder approvals merely by toggling `enabled`.

## New controls

- Private non-secret `hosted-policy.json` in the configured runtime: owner0700 directory, regular0600 single-link file, no file symlink, at most16KiB read even if the file grows. Schema rejects unknown keys/secret fields/custom commands. Trusted runtime aliases are canonicalized for private stores; later retargeting invalidates approval.
- Explicit work or exact-fixture `synthetic-only` mode, allowed workspaces, optional nonempty source path/full-file-hash and prompt-hash restrictions (both required in synthetic-only mode), model/disclosure/version, credential source and reuse approval. Empty allowlists never mean allow-all. Synthetic-only requires fixtures; no automatic claim that their content is non-sensitive.
- Fresh UTC approval and expiry, maximum31day lifetime. Fingerprint checked repeatedly; any changed, withdrawn, expired, insecure or retargeted policy denies the loaded approval. Reload is required to adopt a new revision; there is no default credential reuse.
- Explicit environment-key or stored OpenCode OAuth selection. No fallback to the old Go API key/environment key for OAuth mode; unavailable/near-expiry credentials fail closed. No automatic login/refresh. Credential resolver tests use fake callbacks only.
- Required UTC daily reserved-attempt budget1–100, global to this private runtime, retained across tasks/app recreation/policy identity changes. Exclusive slots and file/ancestor fsync precede dispatch. Failed/partial/unknown reservations are spent, never refunded or deleted automatically. This is not an account-wide or dollar spend ceiling.
- Policy/cancellation rechecked after quota reservation before HTTP dispatch. Existing source/consent/no-replay/completion/draft-only boundaries remain intact. Original generic Start remains fail-closed; separate hosted consent is still required.

See `integrations/paseo-knowledge/HOSTED-OPERATOR-POLICY.md` for exact semantics and activation prerequisites.

## Verification

- Typecheck: PASS.
- Full offline suite: **239/239 PASS**,20 test files (baseline206 plus33 new tests).
- Plugin project audit: **0 vulnerabilities reported**, dependencies/lock unchanged.
- `git diff --check`: PASS.

Tests cover missing/disabled/invalid private policy, consent/credential requirements, UTC expiry, file permissions/link/size constraints, mode/fixture/workspace restrictions, policy revision/expiry/reload, denied source reads before credentials, failed/partial budget markers, concurrent quota reservation, UTC midnight rollover, all-ancestor flush order and unsupported file/directory/parent fsync failure, policy revocation while quota is pending, restart/policy-ID budget persistence, OAuth-kind/expiry refusal, and private runtime alias handling. All data, credential hooks and transports are synthetic; no live inference was added to the prior spent one-shot authorization.

Self-review identified and fixed ambiguous empty-work-allowlist semantics; explicit empty lists now reject. New source functions separate file/config admission from network enforcement. Independent managed reviewer approved bounded DEFAULT-DISABLED deployment with no HIGH/blocking source finding. Tests were inspected, not run independently. A wording caveat was corrected: formatting-only changes do not revoke the normalized configuration fingerprint. Activation, host remediation and native/live validation were not approved by this review.

## Separate host finding

Read-only metadata inventory of the signed `/Applications/Paseo.app` found one bundled Zod package, version **4.4.3**, in Paseo0.15.4. That version is within the published affected range of GHSA-mrhx-c7xc-m3w5 (fixed4.6.6). This is a version-based finding, not a full host audit, demonstrated exploitability or vendor-backport assessment.

The official latest-release API also reported v0.15.4. No official newer release was observed at the check. Do not edit app.asar or invalidate the signed app to patch it casually. A vendor/remediation or explicitly documented host-risk assessment is a separate production gate; the plugin's patched dependency/audit result does not fix this copy.

## Still open

1. Independent review, then explicitly authorized Git delivery/deployment of this candidate.
2. Host security finding disposition and actual external-device authentication evidence.
3. Owner selection/approval of ongoing account, disclosure, model, workspace/fixtures, expiry and limits. No real config is created here.
4. Separately authorized controlled synthetic end-to-end execution plus native/iPhone consent, cancellation, close/reopen/reconnect and unknown-outcome recovery acceptance.
5. Paseo's stale provider-subagent blue badges: all seven old native IDs return not_found, but the retained UI status differs. No signed-app fix/root-cause claim is included in this plugin change.

At the implementation checkpoint installed source was `f0796fa`; hosted execution remains disabled unless a separately approved operator policy is installed. The earlier one-shot synthetic provider smoke is complete and its authorization remains spent. Do not reuse it for activation or another request.

## Delivery review addendum

The initial managed reviewer and a second narrow follow-up reviewer approved DEFAULT-OFF deployment with no HIGH/blocker; both were archived. The follow-up covered synthetic-mode prompt-hash restrictions before credential/quota access. Parent final typecheck and239/239 tests passed. No live operator policy file was present; the existing runtime mode0755 is deliberately not changed here, so activation also requires securing that directory. The signed-host Zod finding remains a separate gate.

## Final test stability addendum

One prior run timed out in the existing preview-admission concurrency test. It combined wall-clock preparation timestamps, an injected clock and a20ms deadline. The test now starts with fake timers and advances its scheduler explicitly, preserving the same admission/late-reader assertions. Two sequential final complete runs passed239/239. This is a test-only stabilization; runtime logic was not weakened.
