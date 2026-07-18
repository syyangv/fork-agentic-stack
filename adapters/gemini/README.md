# Gemini CLI adapter

## Install
```bash
./install.sh gemini
```

Or on Windows PowerShell:
```powershell
.\install.ps1 gemini C:\path\to\your-project
```

## What it wires up
- `GEMINI.md` — Gemini CLI reads this natively as project context. It points
  Gemini at the shared `.agent/` brain and preserves the same recall /
  memory discipline as the other adapters.
- `.gemini/settings.json` — project-level hooks for:
  - `BeforeAgent` task-start correlation
  - `BeforeTool` bounded pre-tool capture
  - `AfterTool` on `run_shell_command|replace|write_file|write_todos`
  - `SessionEnd` bounded behavioral finalization followed by `auto_dream.py`
- `.gemini/skills/` — Gemini scans this path for project skills. The installer
  merges shared skills from `.agent/skills/` into a real directory instead of
  a symlink, because Gemini's skill discovery can miss symlinked directories.
  Pre-existing Gemini-local skills in `.gemini/skills/` are preserved.

## Notes
- If `GEMINI.md` or `.gemini/settings.json` already exists and does not
  reference `.agent/`, the installer will not overwrite it blindly. It will
  print a merge snippet and `./install.sh doctor` will show the adapter as
  yellow until you merge it.
- The post-tool hook normalizes Gemini tool names and payloads into the same
  episodic memory pipeline used by Claude Code, so recurring shell / edit
  work can still promote into shared lessons.
- Gemini only loads project hooks and project skills in a trusted workspace.
  On this Mac, paths under trusted parent folders already work automatically.
  For headless runs in an untrusted location, Gemini itself still requires
  `GEMINI_CLI_TRUST_WORKSPACE=true` or an interactive trust step.
- Special case: if the workspace root is your home directory, the project-level
  `.gemini/` path overlaps with Gemini CLI's usual home-level state directory.
  This adapter is intentionally merge-friendly in that case: keep auth/state,
  add hooks, and merge shared skills without deleting Gemini-local ones.

## Verify
Run Gemini in the project, execute one shell command or file edit, then check:

```bash
tail -1 .agent/memory/episodic/AGENT_LEARNINGS.jsonl | python3 -m json.tool
```

You should see an entry with `"skill": "gemini"` and a non-empty
`reflection`.
