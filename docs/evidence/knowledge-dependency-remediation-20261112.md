# Knowledge dependency remediation — 2026-11-12

## Scope

Branch `fix/knowledge-dependency-advisories`, based on merged PR #52 (`d9529f0`). No commit, merge, deployment, runtime reload, credential export, real provider call or hosted execution enablement in this implementation turn.

## Dependency changes

| Package | Before | Resolved after |
|---|---|---|
| Zod | 4.6.5 | 4.6.6 |
| Vitest / mocker | 4.1.11 | 4.1.12 |
| Vite | 8.3.0 | 8.3.8 |
| shell-quote | 1.10.0 | 1.12.0 |
| brace-expansion | 1.1.18 | 1.1.22 |
| source-map-js | 1.2.1 | 1.2.2 |

Root minimum ranges become `zod: ^4.6.6` and `vitest: ^4.1.12`. SDK 0.8.0, React 19.1.0 and React Native 0.81.5 stay unchanged. No direct transitive dependencies, overrides, force flags or legacy-peer bypasses were added.

The lockfile includes coordinated Vitest/Vite/Rolldown tooling updates (23 existing-package version changes) and 30 optional platform/WASM metadata entries. Added lock entries are optional; no paths were removed. Complete version changes are recorded in the logs, rather than treating this as a two-package-only lock diff.

## Resolver and reproducibility

npm 10.9.9 failed with an internal Arborist `null.edgesOut` error in both the existing checkout and a fresh export, before manifest/lock changes. This does not establish stale node_modules as the cause. A temporary `npm@11.21.2` CLI (compatible with Node 22.23.4) resolved the targeted updates without changing global npm or bypassing peer checks. Install scripts were disabled.

The resulting lockfile then installed successfully with ordinary npm 10.9.9 `npm ci --ignore-scripts` on ARM64 in a separate export. Lockfile SHA256 is identical in the working and clean-installed copies:

`ff17e1d3a8dd63c851fbe03a9f50baf070b914878795c5a336820fa3237367de`

Clean export: `/private/tmp/kv-dependency-clean-geum3vyy`. It contains copied tracked integration/core source plus the new admission test; no personal vault or credential files. It is preserved for reproducibility, not automatically deleted.

## Independent collection admission

Before any Zod schema parsing:

- Hosted task inputs admit only 1–64 sources and at most 64 optional exclusions.
- Stored preparation admits only 1–8 manifest entries and snippets.
- Guards and schemas reuse their limit constants. Existing valid boundary cases remain accepted.

Tests include small oversized fixtures with traversal traps and a mocked schema-parser sentinel. The latter is important: a fixed Zod version must not mask the absence of the independent guard.

Negative control changed only the two guard modules in the disposable export back to the base versions. Three admission tests failed with the expected parser sentinel; the valid-boundary test passed. Both files were restored byte-for-byte in `finally`, then all four sentinel tests passed. The working checkout was not mutated for this control.

## Verification

- Audit before: **7** affected packages — 1 critical, 4 high, 2 moderate.
- Audit after: **0 vulnerabilities reported** for this candidate tree.
- Working checkout: typecheck **PASS**, **206/206** tests across 17 files **PASS**.
- Separate clean ARM64 install: npm ci **PASS**, typecheck **PASS**, **206/206 PASS**.
- Negative control: expected **3 failed / 1 passed**; restored control **4/4 PASS**.
- `git diff --check`: PASS.

Existing deprecation warnings (including inflight/rimraf/glob) are not removed by a zero-advisory audit. Registry results are time-scoped and do not prove general supply-chain or runtime safety.

## Independent review

Initial review was blocked by tool-output compression, and the reviewer correctly withheld approval. On retry using direct source/AST inspection, the reviewer found no HIGH finding or actionable code blocker in this diff. They verified guard ordering/limits, regression coverage, direct dependency floors and lockfile scope. Independent typecheck passed; 38 focused tests passed and one local-server case was sandbox-blocked (listen EPERM), not an observed assertion regression. Parent separately ran all 206 tests successfully with loopback permission in both working and clean-installed copies. Audit-zero, clean-install and negative-control results were not independently reproduced by the reviewer. This is a bounded candidate review, not host-runtime or live-enablement approval.

## Deployment boundary

The installed plugin checkout is still `05184b7`; its observed installed Zod version remains 4.6.5. The candidate's zero-audit result does **not** apply to the installed plugin or Paseo host dependencies. At deployment, verify the actual resolved plugin/host module graph and run the production-auth, provider and native/iPhone gates separately. Hosted execution remains default-disabled; this dependency update does not instantiate the hosted application.

Evidence directory: `docs/evidence/logs/knowledge-dependency-remediation-20261112/`.
