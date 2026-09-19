# Branch Preservation and Worktree Reconciliation Evidence

Date: 2026-09-19
Repository: `https://github.com/syyangv/fork-agentic-stack.git`
Default after cleanup: `master` / `origin/master` at `bb7704e58eb57ab92d7a056e1d9353e829ae3b5c`

## Non-destructive preservation

The private archive root is:

```text
/private/tmp/agentic-stack-branch-archives-20260919
```

It is outside the repository and was not pushed or committed. It has mode `0700`; archive files and manifests have mode `0600`. The archive index records each original path, branch/ref tip, porcelain status, restore diff, archive hash, and included object hashes.

- Archived inactive dirty worktrees: **19**.
- Verified archives: **19/19** archive SHA-256 values and restore manifests valid.
- Symlink policy: `lstat`/archive-link; symlinks were recorded as links and never followed.
- Every archive includes the complete `.agent/memory/` tree, preserving episodic, semantic, candidate, review-queue, and snapshot state.
- Every archive had `source_status_entries=[]`: no uncommitted source-file diff outside `.agent/memory`; dirty state is operational memory/runtime state.
- No archive contents, private memory, credentials, or archive files were added to Git.

The private `index.json` is the authoritative archive/restore manifest. It includes archive SHA-256 values for all 19 snapshots.

## Reconciliation result

| Worktree group | Committed source status | Working-state result |
|---|---|---|
| Adapter-safety, brain-seed-hygiene, Zed | Their committed feature content is represented by merged PR #36/#14; no dirty source diff remains. | Archived local episodic logs, review queues, candidates, rejected candidates, and snapshots. |
| Phase 1–8 and Opus pilot | Their PRs are merged; branch tips are ancestors of current master. | Archived only dirty operational memory/runtime state. |
| Legacy profile / MemOS retirement trees | PRs #19/#21 are merged; branch tips are ancestors of current master. | Archived only dirty operational memory/runtime state. |
| Detached review/attestation trees | No branch source ownership; no active Paseo owner. | Archived dirty memory/runtime state with detached tip recorded. |

## Root cause of cross-worktree writes

The hook implementation intentionally derives `AGENT_ROOT` from the checked-out hook path:

```text
<worktree>/.agent/harness/hooks/<hook>.py -> <worktree>/.agent
```

The Codex/Claude/Gemini/etc. adapters call the shared governed capture path. `record_tool_event()` dispatches successes to `post_execution.log_execution()` and failures to `on_failure()`. Those functions append to that worktree’s `.agent/memory/episodic/AGENT_LEARNINGS.jsonl`; `session_complete.py` appends one final observation. Repeated observations and the dream/review tooling produce `REVIEW_QUEUE.md`, candidate JSON, rejected candidates, and snapshots.

Therefore closed historical sessions leave local operational memory in every worktree they used. This is not a unique source change and is not caused by remote branch ancestry. The behavior is canonical and privacy-redacted; it must not be silently redirected or disabled during cleanup.

## Prevention proposal (not silently applied)

1. Keep hook writes project-local and canonical by default; do not disable hooks or redirect memory implicitly.
2. Make worktree retirement lifecycle-aware: before a Paseo/Git worktree is retired, require an explicit archive manifest and hash verification of `.agent/memory/` plus all dirty paths. Refuse removal when the archive is absent or invalid.
3. Add a read-only reconciliation command that separates source diffs from `.agent/memory` operational state, reports active Paseo ownership, and emits the same restore manifest used here.
4. Treat inactive worktree memory as session-local evidence, not as a reason to publish or merge branch content. Any future central-memory overlay must be explicit and opt-in because it changes canonical memory semantics.
5. Keep `session_complete` and post-tool capture behavior unchanged; prevent accumulation through explicit lifecycle archival, not through hook suppression.

## Proposed retirement scope after targeted approval

Archives are verified, but dirty worktrees were deliberately **not** removed in this pass. The following exact paths can be considered for ordinary owner-approved retirement using the verified archive manifests:

```text
/private/tmp/kv-review-adapter
/private/tmp/kv-review-seed
/private/tmp/kv-review-zed
/Users/syang/.agentic-stack-attest-fix
/Users/syang/.agentic-stack-config-fix
/Users/syang/.agentic-stack-memos-retirement
/Users/syang/.agentic-stack-phase1
/Users/syang/.agentic-stack-phase10
/Users/syang/.agentic-stack-phase2
/Users/syang/.agentic-stack-phase3
/Users/syang/.agentic-stack-phase4
/Users/syang/.agentic-stack-phase5
/Users/syang/.agentic-stack-phase6
/Users/syang/.agentic-stack-phase7
/Users/syang/.agentic-stack-phase8
/Users/syang/.agentic-stack-phase8-opus
/Users/syang/.agentic-stack-safety
/Users/syang/.agentic-stack-seed
/Users/syang/.agentic-stack-zed
```

For branch-attached paths, the proposed follow-up is: verify owner/archive ID, retire the worktree without losing the archive, then use ordinary `git branch -d` where ancestry permits. `agent/phase0-memory-trust` is separate: its local tip `c101aeb1c2f1977c55e46c97e5d090ed3a075186` is not an ancestor of master and requires targeted force-ref-deletion approval; no such deletion was attempted.

Protected and excluded: canonical master, current manager/plugin-source worktree, active manager `9af4da0f-beb6-4c36-8abd-32b8477a88d4`, installed plugin source, and spec/default refs.

Live UI/RPC acceptance remains separate and open.
