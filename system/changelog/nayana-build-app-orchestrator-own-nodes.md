Every node in `plan.json` carries `has_worker`. False means the orchestrating agent does that
node itself, in the integration worktree, and commits it; an interactive node is the only
kind that is false, since it is a question put to the user. A node without a worker carries
no `model` and takes no worker slot, so a wave still launches as many workers as the cap
allows.

`plan.json` also carries `own_groups`: the orchestrator's own nodes grouped into the runs it
should do as one piece of work, with each node's `own_group` naming its run. Two questions in
a row come out as `[[1, 2]]` rather than `[[1], [2]]`, because they are consecutive and both
the orchestrator's, so keeping them apart divides the work between one agent and itself and
invites a second pass over the same files. A run stops only at a node with a worker, which
has to be waited for and merged first. An interactive node stays inside a run -- it is the
same agent asking a question it then acts on -- but fixes an order within it, since the nodes
after the question are written against its answer.

A node the orchestrator does itself still has to write `$RUN/nodes/N/reports/report.md`, the
same as an interactive node does. A later node's task file quotes the report of every node in
its access list and `write-task` fails without it, so a node done but left unreported blocks
everything depending on it.

One known inaccuracy, documented on `schedule_waves`: the wave schedule treats the 5-worker
cap as a batch boundary, while the live loop treats it as a concurrency limit and starts a
held-back node as soon as a slot frees. Where six or more nodes are unblocked at once, the
schedule reports a trailing wave the build would have run alongside the others. Accepted: it
takes six simultaneously unblocked nodes to reach, and a node running with four others
already has every slot the cap allows.
