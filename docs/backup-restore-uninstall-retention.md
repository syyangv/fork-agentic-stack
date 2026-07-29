# Backup, restore, removal, and retention boundaries

This guide documents the recovery and retention behavior implemented in this
checkout. It does not start a provider, change the Phase 8 quality result,
enable evolution, or promote an R7 skill.

## Data boundary

| Data class | Backup / restore | Ordinary transfer | Adapter removal | Destination action |
| --- | --- | --- | --- | --- |
| Governance: preferences, decisions, accepted lessons | Not in the MemOS project-state API | Included by default after validation/bounds | Preserved | Retained or merged |
| Validated evidence ledger | Not in the MemOS project-state API | Included by default after validation/bounds | Preserved | Retained or merged as portable evidence |
| Behavioral project state: profile, DB, WAL/SHM, skills | Whole project state | Excluded | Preserved unless separately managed | Never copied by ordinary transfer |
| CRG graph-derived data | Not in the MemOS project-state API | Never transferred | Not created or deleted | Rebuild locally |
| Other regular files inside the selected MemOS project root | Included in the whole project-state snapshot | Excluded | User-owned content preserved | No transfer action |
| Credentials, environment, logs, process state, queues, journals, backups, evaluation material, prompts/tool output, bytecode, and other runtime outside that project root | Out of scope | Excluded | User-owned content preserved | No transfer action |

The normal bundle contains **preferences, decisions, accepted lessons, and the
validated bounded evidence ledger**. `execute_import_transaction` validates
before writing and rolls back its own bootstrap, adapters, install state,
terminal `AGENTS.md`, and imported data on failure. It does not open, copy, or
rebuild a CRG database. **CRG graph databases, caches, registries, snapshots,
indexes, and derived state are never transferred.** The portable CRG evidence
ledger is not graph state and remains transferable.

Every successful ordinary import returns this instruction: **Rebuild CRG graph
locally after import; no CRG graph database or cache was transferred.** No
universal CRG rebuild CLI is implemented here, so use the destination's
approved CRG workflow after verifying its sources and evidence.

## MemOS project backup and restore

There is no backup or restore CLI subcommand. The public Python APIs are
`memos_backup.create_project_backup(project_root, backup_root, project_id)`
and `memos_backup.restore_project_backup(backup, project_root, project_id)`
in `.agent/memory/orchestration/memos_backup.py`. Use them only from an
operator-controlled Python integration with validated project IDs and
owner-controlled paths.

### Backup

`memos_backup.create_project_backup` takes the stable per-project lifecycle
lock, validates managed configuration and SQLite health, then snapshots the
whole selected project root (regular files and directories only). The snapshot
includes the active managed profile and SQLite WAL/SHM files; it is not an
ordinary transfer artifact. Directories are owner-only (`0700`) and files are
owner-only (`0600`). `manifest.json` uses `agentic.memory.memos-backup.v1`,
inventories the tree with SHA-256 digests, and is verified before atomic
publish.

The lifecycle lock coordinates compliant delivery workers; it does not stop a
bridge. Backup requires quiescent, validated state and never starts or stops a
provider. Unsafe IDs, symlinks, special files, unmanaged configuration, and
unhealthy SQLite data fail closed.

### Restore and recovery

Before `memos_backup.restore_project_backup`, close the bridge and ensure the
project is quiescent. The lifecycle lock excludes compliant delivery workers
but is not a substitute for closing a live bridge. Restore verifies
`manifest.json`, inventory, and SHA-256 digests before touching the target;
then it stages, validates, and performs an atomic swap.

Backup artifacts and the staged-restored tree are owner-only. For an existing
target, the returned **rollback tree** contains the former target; the atomic
rename means the returned rollback tree preserves the former target's existing
modes rather than gaining new owner-only permissions. For a missing target it
returns `None`. Keep its parent owner-controlled, inspect and tighten
permissions if needed, and retain the rollback tree until independent
verification is complete. If the staging swap fails while the target is absent,
restore attempts to put the former target back. Staging cleanup is best-effort,
so inspect the owner-controlled parent after a failure rather than assuming
recovery completed.

Do not substitute a direct live-database file copy: it cannot provide the
manifest, digest, lock, health, consistency, or rollback guarantees above.

## Profiles, Phase 8 block, and adapter removal

Both profiles persist `phase8_quality_gate: blocked`.

- **standard** may contain the reviewed MemOS capability, but it records it
  off: **MemOS capability may be present but remains off**. Shadow/assist is
  rejected, evolution is disabled, and the R7 skill is not promoted.
- **minimal** omits the MemOS provider implementation and is
  **governance-only**. Its orchestration path remains off-safe under the same
  Phase 8 block.

Install, add, reinstall, and upgrade validate profile and blocked state before
mutation. They do not convert a profile in place; this is not a runtime
activation path.

`agentic-stack remove <adapter>` is the implemented adapter-removal surface,
not a whole-stack uninstall. It confirms unless `--yes` is supplied and removes
only installer-owned tracked adapter files. Pre-existing, user-owned, and
shared files are preserved. Last-adapter removal preserves .agent governance
data and profile state; it does not erase memory, reclassify a profile, or
enable a provider. Inspect first with the read-only `agentic-stack doctor`.

## Behavioral export is separate and non-authoritative

Behavioral data is excluded from default scopes. Its explicit CLI route is
`agentic-stack transfer export --behavioral-export`; it requires a project ID,
project provenance, and output location. It delegates to
`export_behavioral_artifact`, requiring lifecycle-lock ownership, a stopped
compliant bridge, validated managed profile/plugin metadata, a SQLite backup
snapshot, bounded inventory/digests, and secret scanning.

The artifact only contains the selected project's bounded database snapshot
and behavioral skills. It excludes configuration, credentials, environment,
logs, process state, queues/journals, CRG state, caches, and evaluation data.
It is **non-authoritative**: there is **no import or activation route**.
Export neither enables MemOS nor changes evolution, the blocked gate, or R7
promotion.

## Immutable R7 evidence boundary

The verified immutable Phase 8 R7 evidence location is
`/Users/syang/.agent/runtime/memos/5efa1310d8759984/evaluation/r7`. Its
owner-only completion backup is
`/Users/syang/.agent/runtime/backups/phase8-r7-complete-20260728T234651Z`.
These locations are historical evidence boundaries, not ordinary-transfer or
behavioral-export input.

No R7 task, verifier, or run identity may be reused as future held-out
evidence. Future experiments need a fresh never-exported corpus and new
approval. R7 remains non-authoritative and activation remains blocked; this
documentation neither authorizes its use nor changes the Phase 8 gate.

## Retention: setting versus policy gap

The generated managed configuration is
`profiles/<project_id>/memos-plugin/config.yaml` and includes
`retentionDays: 30` for file logging. This is a generated provider setting,
**not an enforced system-wide retention policy** for all detailed behavioral
logs or backups.

The Phase 9 operating target for detailed behavioral logs at 30 days and
error/audit summaries at 180 days is a **manual policy gap** here. This
repository has not verified or enforced automatic 30-day cleanup. The
**180-day error/audit-summary retention target is not enforced**, and this
repository provides no automated 30/180-day retention job.

Safe operator actions are read-only `agentic-stack doctor`, reviewing
owner-controlled storage and manifests, and making a verified project backup
before any separately approved retention operation. Deletion schedules,
purges, and logging changes require a tested operator-owned procedure; this
repository supplies no destructive retention automation.

## Failure and handoff checklist

1. Keep the Phase 8 block; recovery must not select shadow/assist, enable
   evolution, or promote R7.
2. For runtime recovery, close the bridge and use manifest-checked atomic
   restore; retain its rollback tree until verification completes.
3. For portable handoff, transfer validated governance/evidence only and
   rebuild CRG locally at the destination.
4. For adapter cleanup, inspect with doctor and remove one adapter; do not
   infer a whole-stack uninstall or data wipe.
5. Treat behavioral exports as review artifacts only; never import them into a
   running project or activate behavioral capability from them.
