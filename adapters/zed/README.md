# Zed adapter

## Install
```bash
cp adapters/zed/.rules ./.rules
```

Or:
```bash
./install.sh zed
```

## What it wires up
Zed's Assistant panel reads a project-root `.rules` file and injects it into
context automatically. No frontmatter/`alwaysApply` toggle is needed — a
single file at the project root is Zed's whole mechanism (unlike Cursor's
`.cursor/rules/*.mdc` directory or Windsurf's dual-file setup).

## Verify
In Zed, open the Assistant panel and ask "What files should you read before
acting?" It should mention `.agent/AGENTS.md`.

## Notes
Zed's project-context feature set is younger and moves faster than Cursor's
or Windsurf's — verify against current Zed docs before assuming this is still
the only/primary mechanism. An earlier draft of this adapter assumed a
`.zed/settings.json` + `.zed/tasks.json` "context server" model instead;
that could not be verified against a real, current Zed config schema, so it
was dropped in favor of the confirmed `.rules` mechanism rather than shipping
speculative config keys.
