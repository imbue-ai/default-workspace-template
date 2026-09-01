---
name: write-imbue-plan-extra
description: >
  Write a routing plan for a build-app request. The plan is collected for
  offline analysis and is never read back by anything in this workspace. NOT
  FOR USE BY ANY AGENT IN THIS WORKSPACE: it is passed as a prompt by the
  write_plan.sh script beside it and by nothing else. It lives outside
  .agents/skills/ so that no agent can discover or invoke it.
metadata:
  author: imbue
---

# Write a routing plan that will not be used

Another agent in this workspace is already handling the request below. Your
plan is recorded for offline analysis. Nothing reads it back, no one acts on
it, and the work it describes is being done by someone else right now.

So: change nothing, address no one, ask nothing. You have read-only tools and
one turn with no reply.

## Step 1: read the skill the other agent is following

Read `.agents/skills/build-app/SKILL.md` before you plan anything. It is the
flow the request will actually be built through, and your plan should be a
routing of *that* work.

Then read enough of the workspace to ground the plan in what is already here:

- `.agents/skills/` -- the skills you can route a node to. Read the `name` and
  `description` frontmatter of each; open the ones the request plausibly needs.
- `system/services/` -- the background services already running.
- `system/apps/` -- the apps that already exist.
- `data/` -- the shape of the workspace's stored data.

## Step 2: write the routing-level plan

You are writing a **routing-level plan** for the build-app work you just read.
That means a DAG of subtasks handed out to workers: each node is one piece of
the work assigned to one worker, and the edges are what each node depends on.

Defining a node takes three things, and those three are exactly the three lists
you output:

1. **capability** -- how strong a worker this node is worth.
2. **subtask** -- what that worker is asked to accomplish.
3. **access list** -- which earlier nodes it depends on, and therefore whose
   handoffs it sees.

The plan is at most **5 nodes**. Work out the shape first: what genuinely has
to happen before what, and what is only sitting in sequence out of habit. A
chain where every node depends on the one before it is a shape you have not
thought about.

### What to optimise, in order

1. **The task gets done.** A plan that is cheap and wide and fails is worth
   nothing. When you are unsure, buy the capability.
2. **Cost.** Among plans that will work, prefer the cheaper one.
3. **Parallelization.** Among plans that will work and cost about the same,
   prefer the wider graph.

Never trade down a priority for one below it. Where you did make a trade, say
so in your reasoning.

The three sections below take each of the three node fields in turn.

### Capability

You have a spectrum of general-purpose LLMs to route to, ranging from less to
more capable, where more capable costs more per node. Each node is a judgment
about where on that spectrum the work is worth placing.

You do not pick a model by name. You pick a bucket:

- `low` -- cheap. Mechanical, well-specified work with a clear right answer:
  reformatting, extracting, applying an already-decided pattern, routine
  scaffolding.
- `medium` -- moderate. Ordinary implementation and verification against a spec
  that already exists.
- `high` -- expensive. Genuine design judgment, ambiguous requirements,
  cross-cutting decisions, anything where a wrong call is costly to reverse.

The asymmetry matters: over-buying on a node costs money, while under-buying on
a node that needed the capability fails it and everything downstream of it. So
the buckets are not a budget to spread evenly -- spend where the risk is.

### Subtasks

Most of the planning happens inside the workers, not here. The pool you route
to reaches up to frontier models, so a worker can take an objective and do its
own decomposition, research and design before it acts. Your plan decides what
work exists and who does it; each worker decides how its own piece gets done.

So write a subtask as an objective: one or two sentences naming what to
accomplish and what to hand back. Add detail only where that detail is a
constraint the worker could not have worked out for itself -- a format a later
node needs, a convention specific to this workspace, a decision already made
upstream. Everything else is the worker's to figure out.

A subtask can ask a worker to do the work from scratch, refine what an earlier
node produced, criticise it, or do something different entirely that makes a
later node easier.

### Nodes that are just a skill or a service

Much of this work already exists here. `.agents/skills/` holds the workspace's
skills and `system/services/` its background services, and build-app itself
leans on several -- the Flask scaffolder script, the `frontend-design` skill
before any markup, `crystallize-creation` at the end. Read what is there before
you write a node that reimplements one.

Routing a node to an existing skill is a good outcome, not a shortcut. When a
node is one, name the skill and stop: the worker running it will follow that
skill's own steps, so re-specifying them in the subtask only invites the worker
to diverge from them.

This also changes the shape of the graph, because some skills are mostly
*waiting*:

- A node that needs a human -- connecting an account, granting a permission,
  putting a credential through the `latchkey` skill -- is blocked on a person,
  not on a model. Its cost is latency.
- Latency-bound nodes belong on an empty access list wherever the work allows,
  so they start at once and the waiting overlaps unrelated work instead of
  being spent alone.

So if an app needs a third-party connection, start the connection node
immediately and let the scaffolding, the icon, or the mock proceed beside it.
Sequencing a human wait behind work that does not depend on it is the most
common way a plan wastes real time.

### The access list -- the ordering of the work

The workers all share one workspace, so whatever an earlier node wrote to disk
is already there for a later one to find. What a node's access list controls is
the ordering and the handoffs:

- **Ordering.** A node waits on the nodes in its access list and on nothing
  else. Nodes that are not waiting on each other run in parallel. Position in
  the lists orders nothing by itself.
- **Handoffs.** A node sees the subtask and the reply of each node it lists.
  That is how one worker learns what another decided, named, or deliberately
  left alone -- the things the files themselves do not say.

What you can write in one:

- `[]` -- an empty list. The node sees only the original request. Two nodes that
  both take `[]` are independent of each other and **can run in parallel**.
- `[0, 2]` -- the node sees nodes 0 and 2. It depends on them, so it runs after.
- `["all"]` -- the node sees everything before it.

Give each node the narrowest access list that still lets it succeed. Narrow
access lists keep context small and widen the graph, so more work runs at once.
Reach for `["all"]` only on a node that genuinely needs the whole history, such
as a final assembly -- it makes that node wait for everything.

## Output

Emit exactly this, and nothing else -- no preamble, no sign-off, no fences
around the whole document. The wrapper writes your stdout to a file verbatim
under a fixed header, so anything that is not the plan ends up in the plan.

```
<thinking>
Why you cut the work where you did, and why each node got the capability
bucket it got.
</thinking>
<output>
capability = ["high", "low"]
subtasks = ["...", "..."]
access list = [[], [0]]
</output>
```

All three lists are the same length -- one entry per node, in order.

