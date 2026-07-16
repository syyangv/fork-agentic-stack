# CRG local hash-drift overlay

The active CRG launcher prepends the local `2.3.6-hash-drift` overlay so
incremental updates compare graph hashes with disk content as well as Git state.
This prevents stale symbols after an uncommitted edit is reverted.

## Retirement gate

Retire the overlay only when all of the following are true:

1. An upstream CRG release contains equivalent disk/hash-drift detection in its
   incremental update path.
2. The mixed-state regression passes against the unmodified upstream release:
   build clean graph, add an uncommitted symbol, update, revert the file without
   committing, update again, and verify the symbol is absent.
3. Full and incremental builds still report nonzero files/nodes/edges for every
   registered active repository.
4. Codex and Claude launch the same pinned upstream version and a fresh MCP
   session passes `list_repos`, update, search, and stale-symbol verification.
5. The overlay directory and launcher remain available for one rollback window;
   removal is a separate, explicitly reviewed change.

Do not retire the overlay merely because CRG was upgraded or because a normal
clean-tree build succeeds. The hash-drift regression is the deciding proof.
