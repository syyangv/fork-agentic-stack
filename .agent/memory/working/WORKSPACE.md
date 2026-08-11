# Working memory

## Goal

Retire the rejected MemOS provider and deploy Governed Memory + Code Evidence.

## Decision evidence

- R7: valid; no success gain; approximately 2.54% median slowdown.
- R8: valid; equal 18/20 success; 5.71% median slowdown; 5.97% p95 regression;
  zero recoveries.
- Complete owner-only archive:
  `~/.agent/archives/memos-retirement-20260811T020000Z`
- Archive SHA-256:
  `7bf3ea62a46297fd527fff5b3d4bc3409016a9fb5d7f339b9a1d3c6f86fbd539`

## Execution state

- [x] Preserve and restore-verify R6/R7/R8 evidence.
- [x] Add TDD retirement plan and transactional migration tests.
- [x] Remove behavioral provider/runtime/model/event-capture surfaces in source.
- [x] Reframe contracts, profiles, CLI, Doctor, dashboard, transfer, and docs.
- [ ] Complete independent review, merge, deploy, retire live state, and verify.
