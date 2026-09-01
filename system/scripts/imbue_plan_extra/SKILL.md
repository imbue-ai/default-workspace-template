---
name: write-imbue-plan-extra
description: >
  Write a routing plan for a build-app request. The plan is collected for
  offline analysis and is never read back by anything in this workspace. It is
  passed as a prompt by the write_plan.sh script beside it, and lives outside
  .agents/skills/ so that it stays out of every agent's skill menu.
metadata:
  author: imbue
---

# Write a routing plan

Another agent in this workspace is handling the request below right now. You are
writing, separately, the plan you would have routed that request through. Your
plan is recorded for offline analysis: it reaches no one and changes nothing.

You have read-only tools and a single turn, so work from what you can read.

## Step 1: read the work you are routing

Start with `.agents/skills/build-app/SKILL.md`. That is the flow this request
will be built through, and your plan is a routing of that work.

Then read enough of the workspace to ground the plan:

- `.agents/skills/` -- the skills a node can be handed to. Skim each `name` and
  `description`, and open the ones this request plausibly touches.
- `system/services/` -- the background services already running.
- `system/apps/` -- the apps that already exist.
- `data/` -- the shape of the workspace's stored data.

## Step 2: write the plan

The plan is a DAG of subtasks handed out to workers. Each node is one piece of
the work assigned to one worker, and the edges are what each node depends on.
Use at most 5 nodes.

Three things define a node, and they are the three lists you output:

1. **capability** -- how strong a worker the node is worth.
2. **subtask** -- what that worker is asked to accomplish.
3. **access list** -- which earlier nodes it depends on, and whose handoffs it
   sees.

Work out the shape first: what has to happen before what, and what can happen
side by side.

### What to optimise, in order

1. **The task gets done.** Where you are unsure, buy the capability.
2. **Cost.** Among plans that will work, prefer the cheaper one. Capability is
   one input to cost, and the tokens each node reads are another: a wide access
   list ships a long history into every node that carries it, and a context
   re-read down a chain is paid for on each node that reads it. A careful
   `high` node can cost less than several `low` ones that each drag the whole
   history along.
3. **Speed.** Among plans that will work and cost about the same, prefer the
   one that finishes sooner. That usually means more work running side by side,
   though a wider graph is worth it only when it actually shortens the run.

Where you traded one of these against another, say so in your reasoning.

### Capability

You route to a spectrum of general-purpose LLMs, from less to more capable,
where more capable costs more per node. Each node is a judgment about where on
that spectrum the work belongs, expressed as a bucket:

- `low` -- mechanical, well-specified work with a clear right answer:
  reformatting, extracting, applying a decided pattern, routine scaffolding.
- `medium` -- ordinary implementation and verification against a spec that
  already exists.
- `high` -- genuine design judgment, ambiguous requirements, cross-cutting
  decisions, anything a wrong call is expensive to reverse.

The two mistakes cost differently. Over-buying on a node costs money;
under-buying on a node that needed the capability costs that node and everything
downstream of it. Spend where the risk is.

### Subtasks

Write a subtask as an objective: one or two sentences naming what to accomplish
and what to hand back. Add detail where it is a constraint the worker could not
have reached on its own -- a format a later node needs, a convention specific to
this workspace, a decision already made upstream. The rest is the worker's to
work out.

Most of the planning happens inside the workers. The pool you route to reaches
up to frontier models, so a worker takes an objective and does its own
decomposition, research and design before it acts. Your plan decides what work
exists and who does it; each worker decides how its own piece gets done.

A subtask can ask a worker to do the work from scratch, refine what an earlier
node produced, criticise it, or do something else that makes a later node
easier.

Nodes take many shapes, and which ones appear follows from the request. A
dashboard over someone's Slack account needs that account connected and its data
shape confirmed; a self-contained tool over data already here needs neither. So
read the following as a sample of the space rather than a checklist, and let the
request decide.

Most of it comes from build-app itself: settling the defaults and the small
plan, pre-flight like picking a DNS-safe name and a free port, drawing the icon,
running the scaffolder and bringing the program up under supervisord, wrapping a
pre-existing server through the escape hatch where scaffolding does not apply,
designing the on-disk store under `DATA_DIR`, building the throwaway mock and
then the real page, implementing routes and persistence, verifying with a request
and a browser assertion, diagnosing what the verification turns up, surfacing the
tab, and assembling the handoff. The rest comes from whatever a given request
drags in: connecting an account through the `latchkey` skill, fetching real data
and confirming its shape, formatting output into the form the next node needs.

With 5 nodes and more shapes than that available, most nodes combine several --
one node routinely decides, builds and verifies. Cut where the work genuinely
changes hands.

The main agent owns the conversation with the user, so a node produces the thing
the user is shown and hands it up there. The two points where build-app waits on
the user -- the mock, then the working site -- still order the graph: work behind
one begins once it clears.

### Handing a node to a skill

Much of this work already exists here, and build-app reaches for several skills
as it goes: the Flask scaffolder script, `frontend-design` before any markup,
`use-ai-integration` when the app itself calls a model, `manage-layout` to place
the tab. Check what exists before writing a node that would rebuild one.

When a node is a skill, name the skill and stop. The worker follows that skill's
own steps, so the subtask stays at the level of which skill, and to what end.

### Scope

Route the work build-app does, and let your plan end where build-app ends. Two
neighbours own the rest, and their work stays out of your plan:

- `crystallize-creation` takes over once build-app hands off the confirmed app,
  and owns the tracking ticket, the hardening pass and the review gates. Making
  that handoff can be the tail of your last node; everything past the handoff
  belongs to that skill and gets no node here.
- `update-app` owns modifying and removing an app, and gets no node here either.

Leave both of them out of the plan entirely. A node spent on either one routes
someone else's work, spends part of the same 5-node budget, and describes
something these workers were never going to do.

### The access list

The workers share one workspace, so whatever an earlier node wrote to disk is
there for a later one to find. A node's access list controls two things:

- **Ordering.** A node waits on the nodes in its access list. Nodes that are not
  waiting on each other run in parallel.
- **Handoffs.** A node sees the subtask and the reply of each node it lists.
  That is how one worker learns what another decided, named, or deliberately
  left alone -- the things the files themselves leave unsaid.

What you can write in one:

- `[]` -- the node sees the original request alone. Two nodes that both take
  `[]` run in parallel.
- `[0, 2]` -- the node sees nodes 0 and 2, and runs after them.
- `["all"]` -- the node sees everything before it, and waits for all of it.

Give each node the narrowest access list that lets it succeed. Narrow lists keep
each node's context small, which is where much of the token cost sits, and they
free nodes to run at the same time. `["all"]` suits a node that genuinely needs
the whole history, such as a final assembly.

Some nodes are mostly waiting. Connecting an account or granting access to a
third-party service -- the `latchkey` skill -- costs time: the request goes up
to the user, and everyone waits on them. Put those on an empty access list
wherever the work allows, so the waiting overlaps the pre-flight, the icon, or
the mock.

## Output

Emit exactly this and nothing else. The wrapper writes your stdout to a file
verbatim under a fixed header, so whatever you emit is the plan.

```
<thinking>
Where you cut the work and why, and why each node got the capability it got.
</thinking>
<output>
capability = ["high", "low"]
subtasks = ["...", "..."]
access list = [[], [0]]
</output>
```

All three lists are the same length: one entry per node, in order.
