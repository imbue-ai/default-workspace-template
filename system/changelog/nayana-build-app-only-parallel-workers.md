`plan_orchestration.py parse` takes a new `--only-parallel-workers` flag, off by default.
On, a worker is launched only for a node that runs beside another worker node; a node
alone in its wave is left to the orchestrating agent, which does the subtask in the
integration worktree and commits it.

Every node in `plan.json` now carries `has_worker`. False means the orchestrator does that
node itself -- always true of an interactive node, and with the flag on, true of a node
nothing runs beside. A node without a worker carries no `model` and takes no worker slot,
so a wave still launches as many workers as the cap allows.

For the DAG 0 -> 1, 2, 3 -> 4 -> 5, the flag launches workers for 1, 2 and 3 only, and the
orchestrator does 0, 4 and 5. Nodes 0, 4 and 5 each ran alone before, so each one cost a
git worktree, a `uv sync --all-packages`, an agent cold start and a merge to produce work
nothing overlapped -- setup the orchestrator pays nothing for.

Off is the flow exactly as it ran before, so the two are comparable on a real build. The
flag is a `parse` argument rather than an environment variable, which keeps the choice a
property of the commit a build ran on.
