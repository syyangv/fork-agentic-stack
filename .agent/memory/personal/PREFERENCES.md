# Personal Preferences

> **This file is yours.** It's the one file every new user should customize.
> Preferences are context, not procedure — tell the agent who you are, not
> how to write code.

## Code style
- _(e.g., TypeScript strict mode always)_
- _(e.g., prefer functional patterns over classes)_
- _(e.g., 2-space indentation, no semicolons)_

## Workflow
- Prefer registry-based sharing for skills across Claude/Codex/agentic-stack; do not create symlink or copy mirrors unless explicitly requested.
- When asked to test CLI-driven behavior, treat a command process exiting 0 as insufficient; verify the model/tool actually ran and inspect structured execution fields.
- _(e.g., always run tests before committing)_
- _(e.g., draft PR early, mark ready when CI is green)_
- _(e.g., prefer small PRs over large ones)_

## Constraints
- _(e.g., primary stack: TypeScript, Python, PostgreSQL)_
- _(e.g., deployment: Railway staging, AWS production)_

## Communication
- Be direct and evidence-backed; the user expects high-agency debugging with concrete verification output rather than plausible explanations.
- _(e.g., be direct, skip pleasantries)_
- _(e.g., surface tradeoffs, don't hide them)_
