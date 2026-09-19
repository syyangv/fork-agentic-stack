# Agent Knowledge Vault — Full Branch Inventory

Date: 2026-09-19
Repository: https://github.com/syyangv/fork-agentic-stack.git
Verified remote default: master at `3ec2a051596493a9e6d427c7bb2b127f01236011` at inventory time.
Local canonical master is separately checked out at `7ef5dc7dab039744b475f6fee65c2f2eaf9aa07f`; it was not reset or changed.

## Counts and ownership

- Local heads inventoried: 19.
- Origin remote heads inventoried: 17.
- Fork PR records inspected: 34.
- Worktrees inspected: 23; no worktree was removed.
- Active Paseo ownership: the knowledge-vault workspace/manager only; other branch-backed worktrees are retained regardless of current agent idleness.

## Branch inventory

| Ref | Tip SHA | Scope | Worktree | PR state | Ancestor of origin/master | Decision |
|---|---|---|---|---|---|---|
| `agent/adapter-safety` | `14bdd5c8303bf43cf6716c4f4d8b85a068484042` | local | `/Users/syang/.agentic-stack-safety` | - | no | RETAIN: existing worktree; no worktree removal authorized |
| `agent/brain-seed-hygiene` | `42b8a6f85767a53593fe28f8e7cbcf0e98bcbd3a` | local | `/Users/syang/.agentic-stack-seed` | - | no | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase0-memory-trust` | `c101aeb1c2f1977c55e46c97e5d090ed3a075186` | local | `-` | #1:MERGED | no | RETAIN: unique/uncertain or not proven ancestor of origin/master |
| `agent/phase1-memory-contracts` | `113b17f2b88d4b2af052bd09c512280128c3adb5` | local | `/Users/syang/.agentic-stack-phase1` | #2:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase2-governance-orchestrator` | `c80ee70348a5e29fc3fe6491ea34831a1c0a46e6` | local | `/Users/syang/.agentic-stack-phase2` | #3:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase3-memos-shadow` | `0f8c7c966ff74dac7d0a1f17e8bfd133f511a7d9` | local | `/Users/syang/.agentic-stack-phase3` | #4:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase4-review-fixes` | `cc2c0fe7c5a3ce5bac889cd404bb1f2fc201a200` | local | `/Users/syang/.agentic-stack-phase4` | #6:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase5-crg-evidence` | `6b68e000027ee23d2d77a17ff60def332b4969d1` | local | `/Users/syang/.agentic-stack-phase5` | #7:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase6-federated-assist` | `78374b59ca9e752f195cab8a4c1162f4206de2a0` | local | `/Users/syang/.agentic-stack-phase6` | #8:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase7-promotion-revalidation` | `5538881fea13fa11741a501f5a7f1f5c41c20886` | local | `/Users/syang/.agentic-stack-phase7` | #9:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase8-host-evolution` | `f0fcbd8d7787b63e998626db1be13cf4d91cf56a` | local | `/Users/syang/.agentic-stack-phase8` | #10:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/phase8-opus-pilot` | `26c09149d9a70732314487c5e95f116c31795fb2` | local | `/Users/syang/.agentic-stack-phase8-opus` | #11:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `agent/zed-adapter` | `6dfbf2fdbba56f0d86383d7c9caf71f400b95ea9` | local | `/Users/syang/.agentic-stack-zed` | #14:OPEN | no | RETAIN: open PR #14 |
| `claude/fix-issue-JdKTT` | `a16823cec20b03eb65e79ba04cd6a225079e877a` | origin | `-` | - | n/a | RETAIN: remote-only without conclusive merge/PR evidence |
| `feat/agent-knowledge-vault` | `472676ac1ad1c392f806e2684d4b726029bd64db` | local | `/Users/syang/.paseo/worktrees/16bjydof/agent-knowledge-vault` | #34:MERGED,#33:MERGED,#32:MERGED,#31:MERGED,#30:MERGED,#29:MERGED,#28:MERGED,#27:MERGED | yes | RETAIN: installed plugin source worktree/current manager |
| `fix/governed-hook-alignment` | `1511c659cc2aee71bcf4be78fe9749e0d16e3f55` | local | `/private/tmp/agentic-stack-hook-alignment` | #24:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `fix/memos-deployed-legacy-profile` | `e2b683ba36910668f96d30db7fb2f7323fea6a8d` | local | `/Users/syang/.agentic-stack-config-fix` | #19:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `fix/retirement-readonly-tree` | `473f8a9e3586efb3874bd9ac844c342285fa6e25` | local | `/private/tmp/agentic-stack-retirement-deploy` | #23:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `master` | `7ef5dc7dab039744b475f6fee65c2f2eaf9aa07f` | local | `/Users/syang/.agentic-stack` | - | yes | RETAIN: default/local canonical checkout |
| `refactor/retire-memos` | `571e0dee200ae1207b5746dd3589496a8cc6f888` | local | `/Users/syang/.agentic-stack-memos-retirement` | #21:MERGED | yes | RETAIN: existing worktree; no worktree removal authorized |
| `spec/v0.19-agentic-turn` | `69aac8956249753ceb3f4722b039eeccd6dd9c83` | origin | `-` | - | n/a | RETAIN: protected/unmerged |

## Cleanup decision

At inventory time, only branches marked SAFE CANDIDATE meet the deletion rule: fully merged by PR/ancestry, no open PR, no active owner, and no worktree. Default/protected/release/spec refs, open PRs, unique/uncertain refs, and installed-source worktrees are retained. Any deletion is performed separately with safe local `git branch -d` or normal remote branch deletion; never force deletion.

The current feature worktree and branch remain required by the installed plugin source path. Local generated `.playwright-mcp/`, root `node_modules/`, and integration `node_modules/` are not branch-cleanup targets.

## Cleanup pass result

For this 2026-09-19 full inventory pass, local heads were 19 before and after, and origin heads were 17 before and after: **0 additional refs were deleted** because no remaining ref met every safe-deletion condition. The prior documented deletions were remote agent/phase0-memory-trust and local fix/retirement-npm-symlinks. The remaining local phase0 ref is retained because safe local git branch -d cannot prove it merged into this checkout's local master and force deletion is prohibited; its remote ref is already gone. Worktree-backed merged branches, the open Zed branch, spec/uncertain refs, and the installed feature branch remain protected.
