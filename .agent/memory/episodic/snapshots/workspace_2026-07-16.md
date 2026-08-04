# Workspace (live task state)

> Replace this template on your first real task. The dream cycle auto-archives
> this file after 2 days of inactivity — don't keep long-lived notes here.

## Current task
- Add and verify a native Gemini CLI adapter in agentic-stack. Completed on 2026-05-03.

## Open files
- `adapters/gemini/adapter.json`
- `adapters/gemini/GEMINI.md`
- `adapters/gemini/settings.json`
- `.agent/harness/hooks/gemini_post_tool.py`
- `test_gemini_adapter.py`

## Active hypotheses
- Gemini CLI's project-local `GEMINI.md`, `.gemini/settings.json`, and
  `.gemini/skills` surfaces are sufficient to share the same `.agent` brain
  structure as Claude Code and Codex.

## Checkpoints
- [x] Added a manifest-driven `gemini` adapter with `GEMINI.md`,
  `.gemini/settings.json`, and `.gemini/skills`.
- [x] Added a Gemini `AfterTool` hook that normalizes Gemini payloads into the
  existing episodic memory pipeline.
- [x] Added `hooksConfig` / `skills` settings and switched `.gemini/skills`
  from a symlink to a real mirrored directory.
- [x] Verified with `python3 -m unittest test_gemini_adapter.py
  test_transfer_plan.py test_transfer_cli.py`.
- [x] Verified with live Gemini CLI runs:
  `/private/tmp/agentic-stack-gemini-live-test` with explicit trust env, and
  `/Users/syang/tmp-gemini-live-test` with automatic trusted-parent behavior.

## Next step
Install the new adapter in the intended real project and verify whether any
existing mirrored skills still trigger Gemini frontmatter parse warnings.
