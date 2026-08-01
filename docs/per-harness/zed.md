# Zed setup

## What the adapter installs
- `.rules` at the project root

## Install
```bash
./install.sh zed
```

## How it works
Zed's Assistant panel reads a project-root `.rules` file and prepends it to
the system prompt for that worktree. There is no frontmatter and no
always-apply flag — the whole file is context whenever the panel is open in
this project.

Zed resolves several fallback rule filenames in addition to `.rules`. The
adapter writes `.rules` because it is Zed's own name and does not collide
with the Cursor or Windsurf adapters if you install several in one repo.

## If you already have a `.rules` file
`.rules` is a generic filename, so the adapter uses the `merge_or_alert`
policy rather than overwriting it:

- no `.rules` present — the file is written for you
- `.rules` present and already referencing `.agent/` — left alone
- `.rules` present and not referencing `.agent/` — **your file is kept**
  and the installer prints the block to merge in by hand.
  `./install.sh doctor` keeps flagging the project until you merge it.

## Logging note
Zed has no post-tool hook. Instruct the model (via `.rules`) to call
`memory_reflect.py` after significant actions. If you want automatic
logging, run the standalone-python conductor in parallel as a side channel.

## Troubleshooting
- `.rules` is only read for the project the Assistant panel has open. If it
  seems ignored, confirm the worktree root is the directory holding `.rules`.
- The AI runs commands from the project root, so the `python3 .agent/tools/...`
  paths in the rule file resolve as written.
