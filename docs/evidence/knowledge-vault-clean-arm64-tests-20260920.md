# Knowledge Vault Clean Native ARM64 Verification — 2026-09-20

- Candidate: `af2c471c45d9f4b9b735ec5cf3e88528e8c46dd0`
- Branch: `chore/knowledge-vault-remaining-acceptance` (PR #46)
- Host: macOS Darwin ARM64
- Node: `v22.23.2` (`arm64`)
- npm: `10.9.8`
- Isolated test root: `/private/tmp/kv-clean-arm64-20260920-mfijRd`

## Method

The exact committed `integrations/paseo-knowledge` tree was exported with
`git archive` into a unique temporary directory. Existing worktree
`node_modules`, untracked files, credentials, live plugin state, and vault data
were not copied. All dependency installation and test execution occurred in
the isolated export.

## Lockfile integrity

`integrations/paseo-knowledge/package-lock.json` had the same SHA256 before and
after installation, both in the repository and isolated export:

`549589ff13420ebc568a9a2a05f3583a3b6f5cf109213eea9823e42aea8bb884`

No lockfile mutation occurred.

## Results

### Fresh dependency install

- Command: `npm ci`
- Exit code: `0`
- Installed: 357 packages
- Audit: 0 vulnerabilities
- Native binding present:
  `node_modules/@rolldown/binding-darwin-arm64/rolldown-binding.darwin-arm64.node`
  (`@rolldown/binding-darwin-arm64@1.2.8`)

### Typecheck

- Command: `npm run typecheck`
- Effective command: `tsc --noEmit -p tsconfig.json`
- Exit code: `0`
- Result: **PASS**

### Vitest

- Command: `npm test`
- Effective command:
  `vitest run --config vitest.config.mjs --configLoader runner`
- Exit code: `0`
- Test files: **2 passed / 2 total**
- Tests: **23 passed / 23 total**
- Failed/skipped: **0 / 0**
- Duration: 398 ms

Passing files:

- `tests/client-entry-lifecycle.test.ts` — 1 test
- `tests/paseo-knowledge.test.ts` — 22 tests

## Reconciliation with the preserved-worktree failure

The separate run documented in
`knowledge-vault-plugin-tests-20260920.md` used the existing worktree's stale,
unreinstalled `node_modules`. Typecheck passed there, but Vitest could not start
because the Darwin ARM64 Rolldown native binding was absent; 0 tests ran.

The clean export demonstrates that the committed lockfile at `af2c471` installs
the required native binding on Darwin ARM64 and that the complete 23-test suite
passes. The earlier failure remains valid evidence about stale local dependency
state, not a failure of the committed clean-install candidate.

## Durable logs

Raw command logs are preserved under:

`docs/evidence/logs/knowledge-vault-clean-arm64-20260920/`

- `npm-ci.txt`
- `typecheck.txt`
- `vitest.txt`

## Scope and governance

- No tracked product source, dependencies, or lockfiles were modified.
- The new evidence document and copied logs are documentation artifacts.
- No live daemon/plugin lifecycle operation or RPC write occurred.
- No vault or credential access occurred.
- No commit, push, merge, or installed-source update occurred.
- This closes clean native ARM64 dependency/test evidence only. It does not
  establish installed UI acceptance, functional safe Knowledge Task execution,
  or merge readiness.
