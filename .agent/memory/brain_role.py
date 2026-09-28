"""Refuse to write into a seed brain.
`~/.agentic-stack/.agent/` is a *template* that `harness_manager.profiles.copy_brain`
installs into new projects. It is not a second live brain. But the memory tools
resolve `BASE` from their own `__file__` location, so invoking the repo copy
(`python3 .agentic-stack/.agent/tools/learn.py ...`) silently writes session
state into the template — and that state then ships to every project installed
afterwards, presented as that project's own history.

Observed 2026-09-28: five lessons and a DECISIONS entry written through the repo
path landed in the seed while the live brain at `~/.agent/` was left untouched
and invisible to `recall.py`.

Import and call `require_live_brain(BASE, tool)` at the top of any tool that
writes. A live brain has no `memory/BRAIN-ROLE` file; `copy_brain` never creates
one, so a copied brain is live by construction.
"""
from __future__ import annotations

import os
import sys

MARKER = os.path.join("memory", "BRAIN-ROLE")

# Deliberately not 2: argparse exits 2 on a usage error, and a caller checking
# only the code would read "missing argument" as "seed brain".
EXIT_SEED_BRAIN = 78  # EX_CONFIG, the sysexits.h convention used elsewhere here.

MESSAGE = """\
{tool}: refusing to write into a seed brain.

  brain:  {base}
  marker: {marker}

This is the agentic-stack template, installed into new projects by
copy_brain(). Writing here ships one machine's session history to every
project installed afterwards, as if it were their own.

Use the live brain instead:

  ~/.agent/tools/{tool}  ...

If you genuinely meant to edit the template's curated seed content (the
architectural DECISIONS entries, skill definitions, tool sources), edit it
directly and say so -- that is deliberate authoring, not a session write.
"""


def is_seed_brain(base: str) -> bool:
    return os.path.exists(os.path.join(base, MARKER))


def require_live_brain(base: str, tool: str) -> None:
    if is_seed_brain(base):
        sys.stderr.write(
            MESSAGE.format(tool=tool, base=base, marker=os.path.join(base, MARKER))
        )
        raise SystemExit(EXIT_SEED_BRAIN)
