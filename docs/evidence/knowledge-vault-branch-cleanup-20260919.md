# Knowledge Vault Branch Cleanup Ledger

Date: 2026-09-19
Repository: `https://github.com/syyangv/fork-agentic-stack.git`
Final default: `master` / `origin/master` at `1743c6bcc37a31d5de500c3dc1610dc0867ce47c`

## Before / after counts

| Ref class | Before cleanup pass | After cleanup pass |
|---|---:|---:|
| Local heads | 20 | 17 |
| Origin heads | 18 | 3 |
| Worktree records | 27 (23 retained from prior inventory + 4 temporary review/integration trees) | 21 |
| Open PRs | 0 | 0 |

The 15 remote deletions were guarded by an immediate exact-tip recheck. No master/default, feature/plugin-source, spec, upstream, or active-manager ref was included.

## Retired paths / metadata

- `/private/tmp/kv-integration` — clean, inactive, non-installed-source repair worktree; removed with ordinary `git worktree remove` (no `--force`). Its branch tip `2ef65976883f0209c10d8358d4a0944af0884775` was already an ancestor of master.
- Pruned five missing worktree metadata records, with no filesystem contents to delete:
  - `/private/tmp/agentic-stack-deploy-214`
  - `/private/tmp/agentic-stack-hook-alignment`
  - `/private/tmp/agentic-stack-hook-deploy`
  - `/private/tmp/agentic-stack-retirement-deploy`
  - `/private/tmp/agentic-stack-retirement-release`

No existing dirty or untracked worktree was removed. No stash, reset, clean, auto-archive, or force worktree deletion was used.

## Deleted local heads

| Ref | Original tip | Reason |
|---|---|---|
| `repair/reconcile-fork-branches` | `2ef65976883f0209c10d8358d4a0944af0884775` | Clean integrated repair branch; PRs #36–#38 merged. `git branch -d` succeeded. |
| `fix/governed-hook-alignment` | `1511c659cc2aee71bcf4be78fe9749e0d16e3f55` | PR #24 merged; worktree path was missing and metadata was pruned. `git branch -d` succeeded. |
| `fix/retirement-readonly-tree` | `473f8a9e3586efb3874bd9ac844c342285fa6e25` | PR #23 merged; worktree path was missing and metadata was pruned. `git branch -d` succeeded. |

## Deleted origin heads

All were checked immediately before deletion and deleted with normal `git push origin --delete`; local dirty branches remain where applicable.

- `agent/adapter-safety` — `14bdd5c8303bf43cf6716c4f4d8b85a068484042`; ported/equivalent in PR #36; local worktree retained with dirty memory artifacts.
- `agent/brain-seed-hygiene` — `42b8a6f85767a53593fe28f8e7cbcf0e98bcbd3a`; ported/equivalent in PR #36; local worktree retained with dirty memory artifacts.
- `agent/zed-adapter` — `6dfbf2fdbba56f0d86383d7c9caf71f400b95ea9`; PR #14 and PR #36 merged; local worktree retained with dirty memory artifacts.
- `agent/phase1-memory-contracts` — `113b17f2b88d4b2af052bd09c512280128c3adb5`; PR #2 merged; local dirty worktree retained.
- `agent/phase2-governance-orchestrator` — `c80ee70348a5e29fc3fe6491ea34831a1c0a46e6`; PR #3 merged; local dirty worktree retained.
- `agent/phase3-memos-shadow` — `0f8c7c966ff74dac7d0a1f17e8bfd133f511a7d9`; PR #4 merged; local dirty worktree retained.
- `agent/phase4-review-fixes` — `cc2c0fe7c5a3ce5bac889cd404bb1f2fc201a200`; PR #6 merged; local dirty worktree retained.
- `agent/phase5-crg-evidence` — `6b68e000027ee23d2d77a17ff60def332b4969d1`; PR #7 merged; local dirty worktree retained.
- `agent/phase6-federated-assist` — `78374b59ca9e752f195cab8a4c1162f4206de2a0`; PR #8 merged; local dirty worktree retained.
- `agent/phase7-promotion-revalidation` — `5538881fea13fa11741a501f5a7f1f5c41c20886`; PR #9 merged; local dirty worktree retained.
- `agent/phase8-host-evolution` — `f0fcbd8d7787b63e998626db1be13cf4d91cf56a`; PR #10 merged; local dirty worktree retained.
- `agent/phase8-opus-pilot` — `26c09149d9a70732314487c5e95f116c31795fb2`; PR #11 merged; local dirty worktree retained.
- `fix/memos-deployed-legacy-profile` — `e2b683ba36910668f96d30db7fb2f7323fea6a8d`; PR #19 merged; local dirty worktree retained.
- `claude/fix-issue-JdKTT` — `a16823cec20b03eb65e79ba04cd6a225079e877a`; no open PR/owner/worktree, actionable-doctor equivalent ported in PR #36.
- `repair/reconcile-fork-branches` — `2ef65976883f0209c10d8358d4a0944af0884775`; PRs #36–#38 merged and clean worktree retired.

## Residual blockers / protected state

- Local `agent/phase0-memory-trust` at `c101aeb1c2f1977c55e46c97e5d090ed3a075186`: remote already absent and PR #1 merged, but safe `git branch -d` still refused because the tip is not an ancestor of current master. No `-D` was used. Further removal needs targeted force-ref-deletion approval.
- Local dirty worktrees remain protected. Exact blocking contents for the three repaired input branches:
  - `/Users/syang/.agentic-stack-safety`: modified `.agent/memory/episodic/AGENT_LEARNINGS.jsonl`, modified `.agent/memory/working/REVIEW_QUEUE.md`, untracked candidate JSON (`8b3e0b2e87dc.json`, `a9d3f0db5991.json`), `memory/candidates/rejected/`, and `memory/episodic/snapshots/`.
  - `/Users/syang/.agentic-stack-seed`: same modified/untracked runtime paths.
  - `/Users/syang/.agentic-stack-zed`: modified episodic log; untracked candidates `34d73602ada4.json`, `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, `b5f78f2e08b3.json`, rejected candidates, and snapshots.
- Other retained local merged branches have the same class of generated dirty memory/runtime state; none had a non-closed Paseo owner in the current ownership inventory. They remain because deleting them would delete untracked/user state, which was explicitly prohibited.
- Current manager/plugin-source worktree remains protected: `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault`, active Luna `xhigh` manager, untracked `node_modules/` and `.playwright-mcp/` preserved.
- Retained remote refs are exactly: `feat/agent-knowledge-vault`, `master`, and `spec/v0.19-agentic-turn`.

Live UI/RPC acceptance remains a separate open criterion and did not block these unrelated safe branch deletions.

## Exact retained dirty/untracked inventory

These existing paths were inspected and intentionally not removed because their contents are dirty or untracked:

| Path(s) | Exact blocking status |
|---|---|
| `/private/tmp/kv-review-adapter`, `/private/tmp/kv-review-seed`, `/private/tmp/kv-review-zed` | modified `.agent/memory/episodic/AGENT_LEARNINGS.jsonl`; untracked `.agent/memory/candidates/rejected/`; untracked `.agent/memory/episodic/snapshots/` |
| `/Users/syang/.agentic-stack-attest-fix` | modified episodic log and `memory/working/REVIEW_QUEUE.md`; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/, snapshots/ |
| `/Users/syang/.agentic-stack-config-fix` | modified episodic log and review queue; untracked candidate `a9d3f0db5991.json`, rejected/, snapshots/ |
| `/Users/syang/.agentic-stack-memos-retirement` | modified episodic log and review queue; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/, snapshots/ |
| `/Users/syang/.agentic-stack-phase1` | modified episodic log; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/ |
| `/Users/syang/.agentic-stack-phase2` | modified episodic log; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/ |
| `/Users/syang/.agentic-stack-phase3` | modified episodic log and `memory/working/WORKSPACE.md`; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/ |
| `/Users/syang/.agentic-stack-phase4` | modified episodic log and workspace/review queue; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/ |
| `/Users/syang/.agentic-stack-phase5` | modified episodic log and workspace/review queue; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/ |
| `/Users/syang/.agentic-stack-phase6` | modified episodic log and workspace/review queue; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/ |
| `/Users/syang/.agentic-stack-phase7` | modified episodic log only |
| `/Users/syang/.agentic-stack-phase8` | modified episodic log and workspace/review queue; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/, snapshots/ |
| `/Users/syang/.agentic-stack-phase8-opus` | modified episodic log only |
| `/Users/syang/.agentic-stack-safety` | modified episodic log and workspace/review queue; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/, snapshots/ |
| `/Users/syang/.agentic-stack-seed` | modified episodic log and workspace/review queue; untracked candidates `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, rejected/, snapshots/ |
| `/Users/syang/.agentic-stack-zed` | modified episodic log; untracked candidates `34d73602ada4.json`, `8b3e0b2e87dc.json`, `a9d3f0db5991.json`, `b5f78f2e08b3.json`, rejected/, snapshots/ |
| Current manager/source `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault` | active manager; untracked `.playwright-mcp/`, `integrations/paseo-knowledge/node_modules/`, `node_modules/` |

## Approved bounded retirement execution — 2026-09-19

The user explicitly approved retirement of the exact 19 paths in the preservation ledger. Immediately before removal:

- 19/19 worktree statuses matched their archived porcelain manifests.
- 19/19 archive hashes/manifests and current file hashes matched.
- 0 active Paseo owners and 0 symlinks were present in the candidate trees.
- Durable private recovery copies were verified at `/Users/syang/.paseo/private/agentic-stack-branch-archives-20260919`; original `/private/tmp` archives remain present.
- All archived tips remain recoverable Git objects; integrated tips are ancestors of current master.

Removed exactly these 19 worktrees, with no extra path:

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

Attached local refs were deleted only with ordinary `git branch -d` after retirement: adapter-safety, brain-seed-hygiene, phase1–phase8/Opus, Zed, `fix/memos-deployed-legacy-profile`, and `refactor/retire-memos` (14 refs). Detached trees had no local branch. `agent/phase0-memory-trust` remains the sole residual local branch requiring targeted force-ref approval; no `-D` was used.

Final counts: local heads **17 → 3**, worktrees **21 → 2**, origin heads unchanged at **3**. Remaining worktrees are canonical master and the active manager/plugin-source worktree. Remaining origin refs are feature/plugin-source, master, and spec. Open PRs remain **0**.
