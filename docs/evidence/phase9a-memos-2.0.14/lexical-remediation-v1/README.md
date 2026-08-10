# Lexical-only remediation evidence

This packet records an isolated MemOS 2.0.14 lexical/FTS-only qualification
rerun. It uses synthetic data only; no R7 or R8 material. The deployed runtime
was unchanged and off; assist and evolution remained disabled.

- Source zero-egress artifact SHA-256: `a893fc6b7ef1d6c7b37298af728e56232de98267ed0077148b621aa5374a4b5d`.
- `zero-egress.json`: fresh, copied-upgrade, and restored-upgrade 2.0.14
  candidate states completed populated turn/retrieval workloads with lexical
  FTS5 retrieval, no model, and no observed Node networking API attempts. The
  legacy 2.0.10 source observation is retained for provenance and is not a
  candidate-state pass. This application-layer tripwire is not packet capture.
- `benchmark/`: raw ledger and summary. Precision@5 and usefulness are `1.0`;
  duplicate logical turns, cross-project leaks, degradation errors, and
  observed egress attempts are zero.
- `reproducible-install.json`: two independent real installs from the pinned 2.0.14 tarball produced identical manifest SHA-256 `dc0aae1417698ed4343895b292fb2f6ac1bcef4820eff6eb46875405b1ed73d9`, exact lexical lock/package hashes, the reviewed distribution marker, no local loader files, and no `@huggingface/transformers`; both installed trees passed validation. Reproduce with `tests/qualification/phase9a_memos_214_reproducible_install.py`.
- `migration/`: isolated migration, backup, restore, and rollback evidence. The
  pristine 2.0.10 rollback uses a schema-valid `local` provider label with
  `embedTraces=false` solely for compatibility and never loads a model. Every
  2.0.14 candidate state is lexical/no-model.

This packet does not authorize deployment, activation, assist, evolution, or
R8. Final qualification still requires the canonical plan's process-level
zero-egress observation and independent review.

## Darwin ARM64-native qualification

The independently attested ARM64 route passed native ABI, lexical benchmark,
migration/rollback, and fail-closed process-trace checks. Sanitized metadata-only
evidence is under `arm64-native-v1/`. The legacy 2.0.10 runtime was limited to
schema, health/session lifecycle, and rollback health; no legacy retrieval or
MiniLM behavior is accepted as qualification evidence.
