# Phase 9A MemOS 2.0.14 qualification evidence index

Date: 2026-08-10
Decision: **LEXICAL REMEDIATION EVIDENCE PASSED; FINAL QUALIFICATION PENDING — STOP before R8**

This index binds the small, reviewable source changes to durable sanitized
evidence in the repository and records the original isolated-run locations.
It does not retain runtime databases. All workloads used synthetic data and
neither R7 nor R8 material.

## Lexical-only remediation rerun

- Durable packet: [`phase9a-memos-2.0.14/lexical-remediation-v1/`](phase9a-memos-2.0.14/lexical-remediation-v1/README.md)
- Supplied zero-egress source SHA-256: `a893fc6b7ef1d6c7b37298af728e56232de98267ed0077148b621aa5374a4b5d`
- Candidate states: fresh 2.0.14, copied 2.0.10 opened by 2.0.14, and restored 2.0.10 opened by 2.0.14 all used lexical FTS5 retrieval, loaded no model, and recorded zero Node networking API attempts.
- Benchmark: precision@5 `1.0`, usefulness `1.0`, and zero duplicate logical turns, cross-project leaks, degradation errors, or observed egress attempts.
- Reproducible install: two real independent `install_verified_tarball` runs from the pinned 2.0.14 tarball produced identical manifest SHA-256 `dc0aae1417698ed4343895b292fb2f6ac1bcef4820eff6eb46875405b1ed73d9`; exact lexical lock/package hashes `acb61ce0d0806fae9fb155cc1fa18cccb8275ffa5f27a0857567f3973f160f92` / `1b6349dcc3fac8cbc27962a00c35b5abbab73a6166c6d28d73db6de55f97a708`; the reviewed distribution marker; no loader files or `@huggingface/transformers`; and two validated immutable installed trees. Durable artifact: `reproducible-install.json`; runner: `tests/qualification/phase9a_memos_214_reproducible_install.py`.
- Migration/rollback: passed in isolated synthetic stores. The pristine 2.0.10 rollback's schema-valid `local` provider label with `embedTraces=false` is compatibility-only and never loads a model; every 2.0.14 candidate state is lexical/no-model.
- Modes: deployed runtime unchanged/off; assist and evolution false; `r8_run=false`. No R7 or R8 material was used.

The application-layer tripwire is not packet capture or native syscall tracing.
Accordingly, the remediation evidence clears the previously failed model-loader
and observable Node-API egress gates, but final qualification remains pending
the plan-required process-level zero-egress observation and independent review.
It authorizes neither deployment nor R8.

## Historical pre-remediation qualification

## Observable egress qualification

- Artifact: `/private/tmp/memos-2.0.14-zero-egress-populated.json`
- SHA-256: `561bbfa8a1aac30defd3acd4aeba4617cd92d01febb88dd6ab2dfbfd9807e7db`
- Runner: `scripts/qualify_memos_zero_egress.py`
- Result: failed. Fresh 2.0.14, copied 2.0.10 opened by 2.0.14, and restored
  2.0.10 opened by 2.0.14 each attempted 12 blocked Hugging Face fetches while
  processing a populated turn and retrieval query.
- The 2.0.10 source baseline made the same 12 attempts. This is shared
  local-embedding remote-fetch behavior, not evidence of a 2.0.14 regression.
- Durable sanitized summaries and a portable checksum manifest are retained in
  [`phase9a-memos-2.0.14/`](phase9a-memos-2.0.14/README.md).

```bash
TMP_ROOT="$(python3 -c 'import tempfile; print(tempfile.gettempdir())')"
python3 scripts/qualify_memos_zero_egress.py \
  --bridge-2-0-14 "$TMP_ROOT/phase9a-real-install/node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs" \
  --bridge-2-0-10 "$TMP_ROOT/phase9a-real-install-2010/node_modules/@memtensor/memos-local-plugin/dist/bridge.cjs" \
  --output "$TMP_ROOT/memos-2.0.14-zero-egress-populated.json"
```

Limitation: the preload tripwire records and rejects Node application APIs
(`dns`, `net`, `tls`, `http`, `https`, `dgram`, and `fetch`). It is not kernel
syscall tracing or packet capture. The result establishes attempted remote
access and blocked completion, not process-level zero-egress.

## Synthetic disabled/shadow benchmark

- Durable directory:
  `tests/qualification/evidence/phase9a-memos-214-synthetic-v2/`
- Files: `summary.json`, `raw-ledger.json`, `egress-attempts.jsonl`, and
  `SHA256SUMS.json`
- Runner: `tests/qualification/phase9a_memos_214_offline_benchmark.py`
- Result: quality/latency pass, privacy STOP. Conventional fixed-denominator
  precision@5 `1.0`; precision among returned hits `1.0`; usefulness `1.0`;
  context bytes p50/p95 `1781/1811`; search p50/p95 `3.049/4.661 ms`; total
  p50/p95 `9.918/14.683 ms`; `turn.start` p50/p95 `5.189/7.774 ms`;
  `turn.end` p50/p95 `1.708/3.277 ms`; duplicates, leakage, and degradation
  errors `0`; blocked Hugging Face fetch attempts `1470`.
- Corpus: 150 synthetic episodes, five categories, 30 evaluation queries,
  exactly five relevant traces per query. All six packet checksums verified.
- Modes: deployed runtime unchanged/off; benchmark isolated shadow-only;
  assist/evolution false; `r8_run=false`.

```bash
TMP_ROOT="$(python3 -c 'import tempfile; print(tempfile.gettempdir())')"
python3 tests/qualification/phase9a_memos_214_offline_benchmark.py \
  --plugin-root "$TMP_ROOT/phase9a-real-install" \
  --work-root "$TMP_ROOT/phase9a-memos-214-synthetic-v2-run" \
  --output tests/qualification/evidence/phase9a-memos-214-synthetic-v2
```

Limitation: this synthetic replay can only qualify preparation for a new
experiment; it cannot authorize activation. The corrected quality and latency
metrics pass, but the no-egress-attempt gate fails, so STOP is solely due to
privacy/egress.

## Migration, backup, restore, and rollback rehearsal

- Durable directory:
  `tests/qualification/evidence/phase9a-memos-214-migration-v1/`
- Canonical artifact: `migration-evidence.json`
- Artifact SHA-256:
  `47c0274fd0f48fddfd72ee7e4fc3ec5643b1dd741a5e4267f1b6c7b7c213481a`
- Checksum manifest: `SHA256SUMS.json` — 11 of 11 entries matched.
- Runner: `tests/qualification/phase9a_memos_214_migration_rehearsal.py`
- Runner SHA-256:
  `66561bf5e893ca410cb09b752e268120b5e05027058215d87fcef44ff8e0aa35`
- Result: passed. All tested database paths returned `quick_check=ok`, retained
  the same 44-table schema and 12 migration records, and preserved the seeded
  canonical content digest across upgrade and restore.
- Marker: 2.0.14 paths contained
  `.migrations/hermes-viewer-port-v1.json` with
  `{"version":1,"migration":"hermes-viewer-port-v1","result":"not-needed"}`
  and SHA-256
  `4ee2ad2157d2103f79db9174855d55248c05e29df1dcb17cc3fa5d16a4192904`;
  2.0.10 rollback paths did not contain it.

```bash
TMP_ROOT="$(python3 -c 'import tempfile; print(tempfile.gettempdir())')"
python3 tests/qualification/phase9a_memos_214_migration_rehearsal.py \
  --plugin-2010 "$TMP_ROOT/phase9a-real-install-2010" \
  --plugin-2014 "$TMP_ROOT/phase9a-real-install" \
  --artifact-2010 "$TMP_ROOT/memos-2010-artifact/memos-local-plugin-2.0.10.tgz" \
  --artifact-2014 "$TMP_ROOT/phase9a-real-install/plugin.tgz" \
  --work-root "$TMP_ROOT/phase9a-214-migration-run" \
  --output docs/evidence/phase9a-memos-2.0.14/migration-rehearsal-run
```

## Required remediation

Bundle and attest the exact local embedding model/tokenizer cache, enforce a
deny-remote/offline mode that fails closed, and rerun all three qualification
lanes. MemOS remains version 2.0.10, off, with assist and evolution disabled;
R8 remains unauthorized.
