The worktree-per-worker variant of `build-app-parallel`, built to be compared
with the shared-folder one on `nayana/build-app-parallel` on a single number:
how long a build takes.

Same flow, same planner, same one-turn orchestration, same `mngr message`
delivery, same lean worker self-checks. What changes is where a worker works and
how its work reaches the next node.

`[create_templates.worktree_worker]` replaces `[create_templates.shared_folder_worker]`:
`transfer = "git-worktree"`, so mngr makes each worker a checkout of its own, and
`worker`'s provisioning comes back (a fresh worktree carries no `.venv`, and
without the sync the worker cold-builds one mid-task at root-closure scope). The
SessionStart `uv sync` in `.claude/settings.json` no longer skips a role, since
every worker here has a venv of its own to converge.

The orchestrator keeps `$BUILD` as an integration worktree on the build branch
and launches each worker with
`--create-arg=--branch --create-arg="build-app-parallel/$APP:mngr/$APP-node-N"`,
which branches that worker off the build branch's current tip. The worker commits
its piece; the orchestrator merges `mngr/$APP-node-N` into the build branch the
moment the worker reports, before launching anything downstream. That merge is
the whole difference: in the shared-folder variant a later node sees earlier work
because it is the same directory, and here it sees it because the branch was
merged first. `create_worker.py` is untouched -- `--branch` reaches mngr through
the generic `--create-arg` passthrough.

Two consequences worth watching in the comparison:

- Two nodes that write the same file now conflict at merge time instead of one
  silently overwriting the other. Nothing is lost, but the orchestrator stops to
  resolve it, and the build waits.
- Each worker pays for its own `uv sync` and plugin install at create time,
  where the shared-folder variant paid once for the whole build. Against that,
  no worker waits on the integration folder's sync before it can start.
