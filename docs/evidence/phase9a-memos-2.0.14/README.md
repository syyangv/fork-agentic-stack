# MemOS 2.0.14 Phase 9A durable evidence

Decision: **FAILED/BLOCKED — STOP before R8**

This directory retains sanitized, substantive evidence summaries without raw
databases, large ledgers, hostnames, user-home paths, secrets, or credentials.
`SHA256SUMS` uses paths relative to this directory and can be checked with:

```bash
(cd docs/evidence/phase9a-memos-2.0.14 && shasum -a 256 -c SHA256SUMS)
```

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

Required remediation is an immutable, integrity-attested local model/tokenizer
cache plus enforced deny-remote behavior, followed by a complete qualification
rerun. The deployed runtime remains MemOS 2.0.10 and off.
