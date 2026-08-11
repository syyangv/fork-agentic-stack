# Governed Memory + Code Evidence

The production architecture has two authority classes:

1. **Governed memory** — permissions, preferences, accepted decisions,
   reviewed lessons, and working state under `.agent/`.
2. **Rebuildable evidence** — current code structure, tests, hashes, and
   optional Code Review Graph results.

```text
request
  -> permissions/preferences/decisions/reviewed lessons
  -> bounded lexical SQLite FTS recall
  -> optional current CRG evidence
  -> bounded context packet
  -> execution
  -> candidate staging and human review
```

Governance is authoritative and always precedes evidence. Evidence is
revision-bound and never promoted into a rule automatically. Candidate lessons
remain staged until a human accepts or rejects them. No hidden, local, or
third-party model path participates in retrieval.

## Behavioral-provider decision

MemOS was rejected after two valid controlled experiments. R7 showed no task
success improvement and approximately 2.54% median slowdown. The lexical-only
2.0.14 redesign in R8 again showed equal success, zero recoveries, 5.71%
median slowdown, and 5.97% p95 regression. Production provider code and state
are retired; the immutable owner-only evidence archive and a minimal decision
record are retained for auditability.

## Portable memory

- `memory/working/` — current task state.
- `memory/episodic/` — append-only execution history.
- `memory/semantic/` — human-reviewed durable lessons and decisions.
- `memory/personal/` — user-specific preferences.
- `memory/candidates/` — staging area, never an automatic authority.
- `memory/evidence/` — bounded revision-linked evidence ledger.

Daily maintenance prepares candidates and review summaries only. It does not
start providers, query models, graduate lessons, or mutate CRG lifecycle.
