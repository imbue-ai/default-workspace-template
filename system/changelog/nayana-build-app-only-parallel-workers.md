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

One known inaccuracy, documented on `schedule_waves`: the wave schedule treats the
5-worker cap as a batch boundary, while the live loop treats it as a concurrency limit and
starts a held-back node as soon as a slot frees. Where six or more nodes are unblocked at
once, a node the schedule shows alone in a trailing wave can lose its worker even though
the build would have run it beside others. Accepted: it takes six simultaneously unblocked
nodes to reach, and a node running with four others already has every slot the cap allows.

`plan.json` also carries `own_groups`: the orchestrator's own nodes grouped into the runs it
should do as one piece of work, with each node's `own_group` naming its run. For the DAG
above that is `[[0], [4, 5]]` -- node 0 alone, then nodes 4 and 5 together, because they are
consecutive and both the orchestrator's, so keeping them apart divides the work between one
agent and itself and invites a second pass over the same files. A run stops only at a node
with a worker, which has to be waited for and merged first. An interactive node stays inside
a run -- it is the same agent asking a question it then acts on -- but fixes an order within
it, since the nodes after the question are written against its answer.

A node the orchestrator does itself still has to write `$RUN/nodes/N/reports/report.md`, the same as
an interactive node does. A later node's task file quotes the report of every node in its access
list and `write-task` fails without it, so a node done but left unreported blocks everything
depending on it -- which under this flag is the common shape, since the node that opens a build is
usually the one nothing runs beside.
