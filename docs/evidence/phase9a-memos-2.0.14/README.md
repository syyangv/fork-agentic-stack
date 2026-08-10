# MemOS 2.0.14 Phase 9A durable evidence

Decision: **PHASE 9A QUALIFIED FOR THE REVIEWED DARWIN ARM64 CANDIDATE — STOP before R8**

This directory retains sanitized, substantive evidence summaries without raw
databases, large ledgers, hostnames, user-home paths, secrets, or credentials.
`SHA256SUMS` uses paths relative to this directory and can be checked with:

```bash
(cd docs/evidence/phase9a-memos-2.0.14 && shasum -a 256 -c SHA256SUMS)
```

## Lexical-only remediation rerun

The durable [`lexical-remediation-v1/`](lexical-remediation-v1/README.md) packet
records a passing lexical/FTS-only rerun: all MemOS 2.0.14 candidate states
used no model and produced zero observed Node networking API attempts; synthetic
precision@5 and usefulness were `1.0` with zero duplicates, leakage, degradation,
or egress attempts; and isolated migration/restore/rollback passed. The legacy
2.0.10 provenance state is not a candidate-state gate. Its schema-valid `local`
label with `embedTraces=false` exists solely for isolated rollback compatibility
and never loads a model.

The subsequent Darwin ARM64-native lane establishes the plan-required
process-level zero-egress observation for its separately attested candidate.
Final independent review reproduced the committed two-root manifest comparison
and checked retained raw exports against the normalized packet. STOP before R8;
no deployment, activation, assist, or evolution is authorized.

## Historical failed packet

The files below retain the pre-remediation model-backed qualification failure.

## Files

- `zero-egress-populated-summary.json` records fresh, 2.0.10 baseline,
  copied-upgrade, and restored-upgrade populated-turn/retrieval observations.
  Every state attempted 12 remote Hugging Face fetches. The 2.0.10 baseline
  made the same attempts as 2.0.14, so the result is shared local-embedding
  remote-fetch behavior rather than a demonstrated 2.0.14 regression. The
  Node application API tripwire blocked all attempts; it is not syscall tracing
  or packet capture.
- `migration-rehearsal-summary.json` records artifact digests, database health,
  schema/migration counts, content preservation, and the version-specific
  Hermes viewer-port marker inventory. This lane passed.
- `benchmark-evidence-pointer.json` binds the durable corrected synthetic-v2
  packet under `tests/qualification/evidence/phase9a-memos-214-synthetic-v2/`.
  Its quality and latency metrics pass; its 1,470 blocked fetch attempts keep
  the privacy/egress gate failed.
- `SHA256SUMS` binds the retained summaries with portable relative paths.

The earlier synthetic-v1 metric is superseded and must not be used. The
corrected v2 packet preregisters exactly five relevant traces per query and
uses conventional fixed-denominator precision@5. STOP is solely due to the
egress/privacy failure, not benchmark quality or latency.

The earlier local-model remediation is superseded and prohibited. The reviewed
distribution removes the model loader and uses lexical SQLite FTS5. Its Darwin
ARM64 native ABI, process-level zero-egress, migration/rollback, and synthetic
benchmark lanes passed final independent review. Production remains MemOS
2.0.10 and off; R8 remains a separate, unauthorized decision gate.
