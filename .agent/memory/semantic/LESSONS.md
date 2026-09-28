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

### 2026-09

- A config writer must locate its insertion point by parsing the structure, never by line offset from a matched key  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_7b0b166eb0ad -->
- A validator that finds the artifact it is meant to detect will report healthy; a self-inflicted corruption must be checked with an independent parser  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_46362c7d79a9 -->
- An absent registry entry means unknown, not none; a maintenance job must never write a destructive default derived from missing metadata  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_aa61b7d7e123 -->
- launchctl bootstrap fails with 'Input/output error' when the label is marked disabled in launchctl print-disabled; run launchctl enable gui/<uid>/<label> first  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_9dd075947f63 -->
- A guard job must be tested against the failure shape it exists for, not only the shape seen when it was written  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_62bf46f7aefb -->

### 2026-05

- When sharing skills between Claude, Codex, and agentic-stack, prefer secondary registry lookup at the original skill root over symlink or copy mirrors.  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_2647c12dc81d -->
- For CLI smoke tests, verify structured evidence that the model actually ran, such as nonzero Claude num_turns and duration_api_ms, instead of trusting exit code zero.  <!-- status=accepted confidence=0.6 evidence=1 id=lesson_7b869fa9fde3 -->

### 2026-04

- Always serialize timestamps in UTC to avoid cross-region comparison bugs  <!-- status=accepted confidence=0.46 evidence=1 id=lesson_422695ae5b2d -->
