"""Archive stale working context. Tasks don't span nights by default."""
import os, datetime, shutil

STALE_DAYS = 2

# Written back after archiving. The workspace must always exist: every
# adapter's instructions tell agents to "update .agent/memory/working/
# WORKSPACE.md as you work", and the file is tracked in git, so moving
# it away left a phantom deletion in every installed project as well as
# in this repo. Archiving is meant to clear stale state, not remove the
# place state goes.
FRESH_WORKSPACE = """# Workspace (live task state)

> Replace this template on your first real task. The dream cycle auto-archives
> this file after {stale_days} days of inactivity — don't keep long-lived notes here.

## Current task
-

## Open files
-

## Active hypotheses
-

## Checkpoints
-

## Next step
-
"""


def _seed_workspace(workspace):
    """(Re)create an empty workspace template at `workspace`."""
    with open(workspace, "w", encoding="utf-8") as fh:
        fh.write(FRESH_WORKSPACE.format(stale_days=STALE_DAYS))


def archive_stale_workspace(working_dir, archive_dir):
    workspace = os.path.join(working_dir, "WORKSPACE.md")
    if not os.path.exists(workspace):
        # Nothing to archive, but the workspace should still exist for
        # the next task — an earlier version of this function moved it
        # away without replacement, so installs can be missing it.
        os.makedirs(working_dir, exist_ok=True)
        _seed_workspace(workspace)
        return False
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(workspace),
                                             tz=datetime.timezone.utc)
    if (datetime.datetime.now(datetime.timezone.utc) - mtime).days < STALE_DAYS:
        return False
    os.makedirs(archive_dir, exist_ok=True)
    dest = os.path.join(archive_dir,
                        f"workspace_{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}.md")
    shutil.move(workspace, dest)
    # Leave a fresh workspace behind rather than a hole.
    _seed_workspace(workspace)
    return True
