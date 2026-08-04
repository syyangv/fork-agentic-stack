# Lessons (auto-distilled + manually curated)

> Entries here outlive specific tasks. The dream cycle promotes recurring
> patterns from episodic into this file. Feel free to curate manually —
> delete bad lessons, tighten wording, reorganize sections.

## Seed lessons
- Always read `protocols/permissions.md` before any destructive tool call.
- Write the failing test before writing the fix.
- Log to episodic memory on every significant action, success or failure.
- When a skill has failed 3+ times in 14 days, propose a rewrite.
- Never force push to protected branches under any circumstance.

## Auto-promoted entries will be appended below

### 2026-08

- When a Git or GitHub operation is rejected before the command launches, diagnose the execution approval/control layer separately; repository hooks cannot cause or fix pre-launch policy-schema failures.  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_da2658bd9c09 -->

### 2026-07

- For scoped Git transactions guarded by codex-control-plane-hooks, authorize each exact command in its own backtick code span after the words 'I authorize'; unquoted commands or a backtick around the whole sentence may not produce a usable grant.  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_96f1fd2d737f -->
- In repositories with background commit or sync automation, re-check HEAD, the cached diff, and upstream state immediately before committing or pushing, then verify the intended commit remains in remote history.  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_89de9aaf50f4 -->

### 2026-05

- When sharing skills between Claude, Codex, and agentic-stack, prefer secondary registry lookup at the original skill root over symlink or copy mirrors.  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_2647c12dc81d -->
- For CLI smoke tests, verify structured evidence that the model actually ran, such as nonzero Claude num_turns and duration_api_ms, instead of trusting exit code zero.  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_7b869fa9fde3 -->

### 2026-04

- Always serialize timestamps in UTC to avoid cross-region comparison bugs  <!-- status=accepted confidence=0.46 evidence=1 id=lesson_422695ae5b2d -->
