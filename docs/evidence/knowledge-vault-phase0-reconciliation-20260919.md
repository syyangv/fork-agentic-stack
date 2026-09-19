# Phase0 Branch Reconciliation Evidence

Date: 2026-09-19
Current default before this evidence commit: `master` at `f52815004f46f8c2658c96cacd0c991c64466808`
Historical branch tip: `agent/phase0-memory-trust` at `c101aeb1c2f1977c55e46c97e5d090ed3a075186`
Historical PR: #1 merged at `9e479521dd7674022266f74e83e69568a75c4fac`

## History preservation

The exact branch history was bundled before review:

```text
bundle: /Users/syang/.paseo/private/phase0-memory-trust-c101aeb1c2f1977c55e46c97e5d090ed3a075186.bundle
sha256: fe969d166ca0438cdc103d35db3c5137f7ac3a10c79afcdbba049d06def
mode: 0600
```

`git bundle verify` reported a complete history and the exact branch ref. A separate clone test resolved and opened commit `c101aeb1c2f1977c55e46c97e5d090ed3a075186`; the clone created a remote-tracking ref rather than a local branch, which is expected and does not affect restoration viability.

## Net-content review

The branch diverges from current master at `9424c58e1cfc17a709d1adfa13678e876edfe409`. It contains 22 post-base commits and 82 changed paths. Final-tree comparison classified those paths as:

- 36 equivalent in the current master tree.
- 35 differing because current master has later compatible hardening, retirement, or documentation evolution.
- 11 branch-only paths, all historical runtime memory or superseded guidance/sync paths; none is missing product functionality.

Fresh current-master regressions passed: **38 passed** across phase0 trust, Gemini adapter, upgrade/doctor, and transfer tests. The full current suite remains **487 passed, 1 skipped** from the prior verified master run.

## Disposition by unmatched commit intent

| Historical intent | Disposition | Evidence / action |
|---|---|---|
| `22f3f63`, `d853955` skill-sync and `.agents/skills` retirement | Already present/superseded | Current canonical skill registry is `~/.agent/skills`; current master retains the modern registry contract. |
| `419a685`, `af88910`, `61e8373`, `d7ac42f`, `d6ce70f`, `fd86f1b` skill-inventory/frontmatter/visibility work | Already present or superseded by later inventory baselines | Current skill files, manifests, inventory tooling, and tests are newer/equivalent; no missing source behavior found. |
| `77c262d`, `3030e49`, `25fa9b2`, `ee235b7`, `c101aeb` Gemini hooks, manager integration, duplicate-signal fix, and rename | Already present | Current master history contains the Gemini hook/manager fixes; current adapter uses `GEMINI.md`; targeted Gemini tests pass. |
| `b24597c` Gemini/Codex README wording | Superseded by later current README/docs | Current default includes later adapter and Zed documentation; no functional delta remains. |
| `3827744` Gemini registry-policy learnings | Historical memory only | Deliberately not ported; semantic/episodic/candidate state remains in the private bundle, not Git. |
| `d4afa6b`, `5132614`, `ec6b273` trusted memory orchestration, Phase0 recovery, runtime-ignore hardening | Already present/evolved | Current master contains these fixes plus later retirement and installer hardening; phase0/upgrade tests pass. |
| `e038b4a` Zed adapter plus memory snapshot | Already delivered | Zed source was reconciled and merged through PRs #14/#36; only historical memory snapshot content was excluded. |
| `589ba9d`, `af58260`, `5abf97c`, `4896a97`, `8591e89`, `4e0fc24`, `6ea20d6`, `a26837c` top-level guidance mirrors, context-sync, Paseo duplicate purge, and auto-commits | Superseded; not ported | Guidance-sync audit confirms `~/.agent/AGENTS.md` + `~/.claude/CLAUDE.md` are the canonical pair, with Codex as a thin layer. Current master intentionally has no new root guidance policy source. Porting these would add a competing guidance owner or broad home-file mutation. No permissions/CI/security policy was changed. |
| `b24597c` through `c101aeb` final docs/adapter changes | Already represented or superseded | No genuinely missing code or targeted regression was identified after current-tree comparison and tests. |

## Final decision

No implementation change is required. Porting the historical branch wholesale would reintroduce runtime memory, stale guidance mirrors, and obsolete architecture. The substantive phase0 functionality is present in current master through direct or equivalent later commits. The branch is therefore safe to retire only after the preserved bundle/recovery artifact is retained.

The local phase0 ref remains at `c101aeb...` because safe `git branch -d` previously refused its non-ancestor history. No force deletion was performed. A targeted force-ref deletion decision is the only remaining phase0-specific Git action; it is not a content blocker.

Live plugin UI/RPC acceptance remains a separate open criterion.
