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

Write a subtask as an objective: one or two sentences naming what to accomplish
and what to hand back. Add detail only where that detail is a constraint the
worker could not have worked out for itself -- a format a later node needs, a
convention specific to this workspace, a decision already made upstream.
Everything else is the worker's to figure out.

That is because most of the planning happens inside the workers, not here. The
pool you route to reaches up to frontier models, so a worker can take an
objective and do its own decomposition, research and design before it acts.
Your plan decides what work exists and who does it; each worker decides how its
own piece gets done.

A subtask can ask a worker to do the work from scratch, refine what an earlier
node produced, criticise it, or do something different entirely that makes a
later node easier.

Nodes take many shapes. A non-exhaustive list, drawn from the work build-app
actually covers: settling the defaults and the small plan, pre-flight like
picking a DNS-safe name and a free port, drawing the icon, running the
scaffolder and bringing the program up under supervisord, designing the on-disk
store under `DATA_DIR`, connecting an account when the app needs a third-party
service, fetching real data and confirming its shape, building the throwaway
mock and then the real page, implementing routes and persistence, setting up a
scheduled refresh, verifying with a request and a browser assertion, diagnosing
what the verification turns up, surfacing the tab, formatting something into the
shape the next node needs, and assembling the handoff.

With at most 5 nodes and more shapes than that available, most nodes combine
several -- one node routinely decides, builds and verifies. Cut on where the
work genuinely changes hands, never to collect shapes.

Do not write nodes around talking to the user: the main agent owns that
conversation, not the workers, so a node produces the thing the user is shown
and stops there. The two points where build-app waits on the user -- the mock,
then the working site -- still order the graph, since nothing behind one can
start until it clears.

A background component is its own decision, and the default is that there is
not one. In preference order: an app that does its work on request needs
nothing; an app that needs periodically refreshed data wants a scheduled
automation writing into its data dir; only a genuinely continuous need earns a
supervisord program of its own. Do not plan a background component the request
has not asked for.

### Nodes that are just a skill or a service

Much of this work already exists here. `.agents/skills/` holds the workspace's
skills and `system/services/` its background services, and build-app reaches
for several as it goes: the Flask scaffolder script, the `frontend-design`
skill before any markup, `use-ai-integration` when the app itself calls a
model, `manage-layout` to place the tab. Read what is there before you write a
node that reimplements one.

Routing a node to an existing skill is a good outcome. When a node is one, name
the skill and stop: the worker running it will follow that skill's own steps,
so re-specifying them in the subtask only invites the worker to diverge.

### Stay inside build-app's scope

Route the work build-app itself does, and stop there. Some skills sit on the
other side of its boundary and are not yours to plan:

- **What build-app hands off.** It finishes by handing the confirmed app to
  `crystallize-creation`, which then owns everything after -- the tracking
  ticket, the hardening pass, the review gates. Making the handoff can be the
  tail of your last node; what happens on the other side of it is not a node.
- **What build-app routes elsewhere.** Modifying or removing an app belongs to
  `update-app`, not here.

Absorbing either is planning someone else's work, and the nodes you spend on it
are not real -- they eat the 5-node budget without moving this task forward.

### Nodes that are mostly waiting

Some work in scope is blocked on a person rather than on a model. Connecting an
account or granting access to a third-party service -- the `latchkey` skill --
costs latency, not capability: the request goes up to the user and then
everyone waits on them.

Put those on an empty access list wherever the work allows, so they start at
once and the waiting overlaps unrelated work instead of being spent alone. If
an app needs a third-party connection, start the connection node immediately
and let the pre-flight, the icon, or the mock proceed beside it. Sequencing a
human wait behind work that does not depend on it is the most common way a plan
wastes real time.

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

