# MemOS retirement implementation plan

## Decision

R7 and R8 were valid negative experiments: MemOS produced no success gain and
added latency. Production is therefore reframed as **Governed Memory + Code
Evidence**. MemOS is rejected, not a dormant default.

## Test-first sequence

1. Preserve an owner-only, hash-verified archive of R6/R7/R8 evidence,
   preregistrations, raw results, reviews, analyzers, deployment/rollback
   records, conclusions, and decision history. Prove a clean read/restore.
2. Add retirement contract tests that require governance + evidence-only
   profiles/configuration and reject MemOS/model/provider fields.
3. Add transactional retirement tests covering backup, rollback,
   concurrent edits, symlinks, partial failure, and user/CRG preservation.
4. Remove provider runtime, bridge, lifecycle capture, assist/evolution,
   promotion, provider backup/export, installer, Doctor/status/dashboard,
   schemas, dependencies, and scheduled integration.
5. Rewrite architecture, operations, transfer, and retention documentation;
   retain only the minimal rejection record and immutable evidence index.
6. Run focused, full, cross-platform, secret, dependency, and CRG-drift
   checks. Obtain independent review and merge the exact reviewed head.
7. Back up live state, deploy source, run the reviewed retirement transaction,
   verify Doctor/schedulers/recall/FTS/CRG, and prove no MemOS process,
   runtime, config, lock, or dependency remains outside the archive.
8. Rehearse rollback without leaving MemOS active; retain the rollback backup.

## Invariants

- Never delete or rewrite semantic/episodic governance memory.
- Never delete CRG databases or user-owned files.
- No local, hidden, or third-party model path.
- Ambiguous ownership is preserved and reported.
- Destructive live cleanup occurs only through the reviewed transaction.
- R6/R7/R8 evidence remains immutable and independently readable.
