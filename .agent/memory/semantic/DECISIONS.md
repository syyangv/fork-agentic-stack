# Major Decisions

> Record architectural or workflow choices that would be costly to re-debate.
> Use this template for each entry:

## YYYY-MM-DD: Decision title
**Decision:** _what was chosen_
**Rationale:** _why, in one or two sentences_
**Alternatives considered:** _what else was on the table and why rejected_
**Status:** active | revisited | superseded

## 2026-01-01: Four-layer memory separation
**Decision:** Split memory into working / episodic / semantic / personal rather than one flat folder.
**Rationale:** Each layer has different retention and retrieval needs. Flat memory breaks at ~6 weeks.
**Alternatives considered:** Flat directory (fails at scale), vector store (over-engineered for single user).
**Status:** active

## 2026-04-26: Add `design-md` seed skill (DESIGN.md / Google Stitch)
**Decision:** Ship a sixth seed skill, `design-md`, that points coding agents at a root `DESIGN.md` (Google Stitch format) as the visual-system source of truth. Loads only when `DESIGN.md` exists at the project root, default behavior is read-only on the contract file, and validation prefers `npx @google/design.md lint DESIGN.md` over hand-checks.
**Rationale:** `DESIGN.md` is becoming a de facto contract for AI-driven UI work; without an explicit skill, agents invent ad-hoc tokens that drift from the user's design system. Gating on `DESIGN.md`-existence keeps the skill silent on projects that don't use the format.
**Alternatives considered:** Bundle the rules into `git-proxy` or `skillforge` (wrong scope, wrong triggers); leave it to per-project `.agent/skills/` overrides (loses the cross-harness benefit); broader triggers like "UI"/"frontend"/"components"/"styling" (too generic, loads on every UI task even without DESIGN.md).
**Status:** active

## 2026-05-17: Skill roots are registries, not mirrors
**Decision:** Keep `.agents`/agentic-stack skills at their original `~/.agents/skills` path, keep Claude-local skills at `~/.claude/skills`, and teach Codex/Claude to consult secondary registries instead of symlinking or copying skills into another harness root.
**Rationale:** Mirroring made ownership ambiguous, caused invalid/stale skill files to appear under the wrong harness, and obscured which system owns a skill. Registry visibility preserves source-of-truth paths while still allowing both agents to use shared skills.
**Alternatives considered:** Symlink `.agents` skills into `~/.claude/skills` and `~/.codex/skills` (rejected: violates original-path ownership and created confusing hosted copies); copy skills between roots (rejected: drift-prone and harder to audit).
**Status:** active

## 2026-05-17: Dynamic CLI tests must prove model execution
**Decision:** Runtime tests for Claude/Codex registry visibility must check the CLI actually produced a model turn/result, not merely that the process exited successfully. Claude checks should fail on `num_turns <= 0`, `duration_api_ms <= 0`, empty result, or quota messages such as “out of extra usage.”
**Rationale:** Claude Code can return a successful process with zero usage/zero turns when quota or non-interactive execution short-circuits. Treating exit code 0 as success created a false pass for the registry teaching test.
**Alternatives considered:** Manual spot-checking `claude -p` output (rejected: easy to miss zero-turn JSON); relying only on static instruction file inspection (rejected: does not prove the CLI loaded the guidance).
**Status:** active

## 2026-09-28: Scheduled-task scripts are version-controlled in ~/dotfiles, not only their plists
**Decision:** `~/dotfiles` now tracks `macos/library-scripts/` (the `~/Library/Scripts` bodies of the launchd jobs) alongside `macos/launchd/` (the plists), via the existing copy-out/copy-in pattern with no symlinks. `sync.sh` copies the scripts daily at 04:30 filtering `*.bak*`; `install.sh` restores them executable and lints every plist afterward. The `paseo-guard` split stays deliberate: plist in `~/dotfiles` (machine config), script in `~/.agentic-stack/scripts/` (source that also ships upstream). Excluded from tracking: `*-workspace/` eval output, `.paseo-managed-files.json` (machine-generated), and plugin checkouts like `ai-desk-card/`.
**Rationale:** `~/dotfiles` tracked 82 plists but none of the scripts they invoke, so a job whose body was missing or unversioned was indistinguishable from a healthy one. That is precisely how `com.syang.agentic-stack.paseo-guard` sat present-but-inert: the plist existed in the repo and on disk from 2026-08-04, but `~/.agentic-stack/scripts/purge_paseo_duplicates.py` did not exist, so nothing purged the retired `~/.agents/` root. It also meant the frontmatter-corruption fix to `skill_inventory_weekly.py` had no version-controlled home.
**Alternatives considered:** Moving the scripts under `~/.agentic-stack/scripts/` and symlinking from `~/Library/Scripts` (rejected: the no-symlink convention, and `~/Library/Scripts` is not a repo so its contents stay unversioned either way); a separate new repo for scheduled scripts (rejected: `~/dotfiles` already exists with a working sync job and the right semantics — this was an omission, not a missing repo).
**How to apply next time:** When a launchd job is reported missing or inert, check the script body exists and is version-controlled before touching the plist. Any new first-party job script belongs in `~/Library/Scripts` so the 04:30 sync picks it up; anything under `~/projects/` is already versioned by its own repo and should not be duplicated here.
**Evidence:** `plutil`-parsed all 64 `com.syang.*` plists; `~/Library/Scripts` held 15 files of which 10 were job bodies, none tracked. Confirmed live: recreating `~/.agents/skills/paseo-help` is purged within one 60s cycle. Commits `4386d8e` (dotfiles), `60c81f4` + `f6602f5` (`~/.agentic-stack`).
**Status:** active

## 2026-09-28: Disk reclamation strategy and the dotfiles retired-plist split
**Decision:** Two rules govern cleanup on this machine. (1) Reclaim by removing what is *reproducible*, not what is irreplaceable: `~/.agent/backups/` keeps all 2,039 payload files, both `MANIFEST.json`, and all 5 `rollback.tar.gz`, but the 6 `node_modules` trees inside the memos payloads (1,068 MB) are gone, each verified to have a sibling `package-lock.json` so `npm ci` restores it. (2) Retired LaunchAgent plists live in `~/dotfiles/macos/launchd-retired/`, never in `macos/launchd/`, because `install.sh` globs the latter; five jobs retired 2026-09-27 were moved out on 2026-09-28. `sync.sh` now warns on any repo-only plist. 267 rejected memory candidates (77 MB) were removed with `git rm`; all were tracked and none are cited by `lessons.jsonl`.
**Why it matters:** The volume sat at 3.6% free (18 GB of 494 GB), and the obvious targets were either the wrong shape or unsafe. `backups/` looked like 1.7 GB of dead weight, but two thirds was third-party packages. Separately, the dotfiles restore path would have re-deployed five deliberately retired jobs — including `com.syang.paseo.daemon`, which conflicts with Paseo.app by design. `launchctl disable` was the only thing preventing the load, which is a single flag away from a failed restore.
**Alternatives considered:** Deleting the whole `backups/` tree (rejected: 700 MB of that is the only copy of the memos retirement payloads and the rollback tarballs); hard-linking the two near-identical 517 MB memos dirs (rejected: they differ — one has `memory/orchestration/providers` the other lacks, and the runtime halves are already node_modules-free); leaving the retired plists in place and relying on `launchctl disable` (rejected: it worked, but it makes correctness depend on a persistent flag surviving a machine restore).
**How to apply next time:** Before deleting a large directory, decompose it — `du -sh` per subtree and look for `node_modules`, `.venv`, `__pycache__`, or build output, and confirm a lockfile sits beside each. Verify deletions by recounting the target pattern afterward, never by the byte total the delete call reported. For any config repo with a restore script, keep retired entries outside the globbed directory. When a config entry is missing from disk but shows no retirement evidence, keep it and surface the question rather than inferring intent.
**Evidence:** Volume at 3.6% free triggered the work. `du` decomposition showed 1,068 MB of 1.7 GB was `node_modules`; 6 of 6 trees had sibling `package-lock.json`; non-`node_modules` provider payload was 1.4 MB. Seven repo-only plists found by diffing `macos/launchd/` against `~/Library/LaunchAgents`; `launchctl print-disabled` confirmed 5 disabled and 2 not. After: 19.0 GB free (3.9%), 208 skills parse, `paseo-guard` loaded, 4 repos at 0 unpushed. Commits `3c47740` (prune), `983f662` + `8ec0d84` (dotfiles).
**Status:** active
