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

## 2026-09-28: `~/.agentic-stack/.agent` is a seed template, not a second live brain
**Decision:** The agentic-stack repo's `.agent/` is the template that `harness_manager.profiles.copy_brain()` installs into new projects. It is marked with `.agent/memory/BRAIN-ROLE` (contents: `seed`), and `.agent/memory/brain_role.py` makes all six writing tools (`learn.py`, `graduate.py`, `reject.py`, `reopen.py`, `list_candidates.py`, `memory_reflect.py`) refuse to write when that marker is present, exiting `78` (`EX_CONFIG`) rather than argparse's `2`. `copy_brain` strips the marker on copy, so a real install is writable. The live brain is `~/.agent/` and has no marker. Session state that had leaked into the seed was reverted to the curated seed content.
**Why it matters:** The tools resolve `BASE` from their own `__file__` location, so `python3 .agentic-stack/.agent/tools/learn.py ...` wrote into the template with no error. That content is shipped to every project installed afterwards and reads as that project's own history. On 2026-09-28 five lessons and two DECISIONS entries accumulated this way while the live brain was untouched — and `recall.py` never saw them, so the split looked like data loss rather than a misdirected write.
**Alternatives considered:** Symlinking `~/.agent` at the repo path (rejected: `copy_brain` refuses an existing destination, and a symlink would make the template and the live brain the same object, so seed edits would mutate real memory); deleting the repo's memory tools (rejected: `copy_brain` ships them, and new installs need them); leaving it documented only (rejected: the failure is silent, so it needs a mechanical stop).
**How to apply next time:** Write memory through `~/.agent/tools/`, never the repo copy. If a tool is added that writes to the brain, call `require_live_brain(BASE, "<tool>.py")` — `tests/test_brain_seed_hygiene.py::test_every_memory_writer_calls_the_guard` fails otherwise. Authoring changes to the seed's curated content (architectural DECISIONS entries, skills, tool sources) are still made directly, and are deliberate edits rather than session writes.
**Evidence:** `profiles.py:copy_brain` and `_ignore_for`; 11 repo tools match the `__file__`-relative `BASE` pattern.
**Correction (same day, two commits later):** The guard was first placed at module import in all six tools, and it broke `recall.py` — which imports `memory_reflect` only to log what it surfaced, inside an `except Exception` block. `SystemExit` is not an `Exception`, so the read died before returning a result while its own log line reported the guard as though nothing were wrong. `list_candidates.py` was also guarded despite making no writes. Both fixed in `28b9b48` / `a208cd2`: the call moved inside `main()`, and read-only tools are no longer guarded. The lesson that generalises: a refusal must be raised from the entry point, never the module body, and a green suite asserting only the new behaviour cannot detect what the new behaviour broke.
**How to apply next time (added):** After adding a guard or a refusal to a shared module, run every tool that imports it — not just the ones the diff names. Keep read-only tools unguarded; `tests/test_brain_seed_hygiene.py::test_read_only_tools_stay_usable_against_the_seed` and `::test_writers_guard_at_entry_not_at_import` now pin both halves.
 `diff -rq` of the two memos dirs, and `git show de65762^:.agent/memory/semantic/DECISIONS.md` to recover the pre-leak seed. Marker propagation was caught by asserting against the real repo brain after the fixture-based test passed; the guard exits 78 after the first exit code collided with argparse. Suite 492 passed, 1 skipped. Commits `90e7bf3` (seed), `bbaec72` (live).
**Status:** active
