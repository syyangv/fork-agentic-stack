# Backup, restore, removal, and retention

## Production data boundaries

| Data | Transfer | Removal | Recovery |
|---|---|---|---|
| Preferences, decisions, accepted lessons | Included after validation | Always preserved | Restore from governance backup |
| Episodic history | Scope-controlled | Always preserved/archive-only | Append-only recovery |
| Candidate lessons | Scope-controlled | Preserved | Human review remains required |
| Bounded evidence ledger | Included after validation | Preserved | Revalidate against current revision |
| CRG databases/caches | Never transferred | Never removed by memory tools | Rebuild locally |
| Experiment archives | Never transferred by default | Preserved owner-only | Verify nested hashes and read procedure |

Ordinary import is transactional. It validates before writing and rolls back
its own bootstrap, adapters, install state, terminal `AGENTS.md`, and imported
data on failure. It never opens, copies, removes, or rebuilds a CRG database.

## Adapter removal

`agentic-stack remove <adapter>` removes only tracked installer-owned adapter
files. Pre-existing, shared, governance-memory, evidence, and CRG files are
preserved. Inspect first with read-only `agentic-stack doctor`.

## Historical behavioral-provider retirement

`agentic-stack retire-memos --yes` is a one-time migration for installations
created before the Governed Memory + Code Evidence architecture. It validates
all known provider-owned paths, rejects symlinks/special files, writes an
owner-only hash manifest backup, removes provider-owned runtime/configuration,
and atomically rewrites installation state. On failure it restores the captured
payload. It does not touch semantic/episodic memory, candidates, evidence
archives, or CRG data.

Keep the produced rollback backup until Doctor, schedulers, lexical recall,
evidence health, and CRG integration have been independently verified.

The complete R6/R7/R8 historical record is outside production runtime at
`~/.agent/archives/memos-retirement-20260811T020000Z`; see
`docs/evidence/behavioral-provider-rejection.md` for its verified digest.
