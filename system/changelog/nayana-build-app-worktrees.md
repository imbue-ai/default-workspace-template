The `build-app` flow now gives each worker a git worktree of its own instead of
running every worker in one shared folder.

A worker is created with `--template worktree_worker` (transfer
`git-worktree`) and `--branch <build branch>:mngr/<app>-node-N`, so its checkout
is branched off the build branch as it stands at that moment: the node sees every
node merged before it, and hands its own work back by committing to its branch.
The orchestrating agent keeps an integration worktree on the build branch, merges
each node's branch the moment that node reports, and serves the user's previews
from there.

What follows from that:

- Workers commit. Their branch is what gets merged, so uncommitted work never
  reaches the build. The orchestrator no longer commits the folder itself.
- The merge is the one piece of bookkeeping that cannot be deferred: a node
  launched off an unmerged build branch cannot see the work it depends on.
- Two nodes writing one file is a merge conflict rather than a silent overwrite.
  Nothing is lost -- both versions are on their own branches -- but the build
  waits while the orchestrator resolves it.
- Each worker provisions itself (`uv sync --all-packages` as part of its create),
  because a fresh worktree carries no `.venv`. The SessionStart sync hook no
  longer skips these agents.
- A failed `mngr create` is no longer dangerous. Its cleanup removes the worktree
  it made, which here is the worker's own and empty; the integration folder was
  never handed to a create, and every finished node is a commit on the build
  branch.

The shared-folder variant is on `nayana/build-app-parallel`. The two exist to be
compared on latency.
