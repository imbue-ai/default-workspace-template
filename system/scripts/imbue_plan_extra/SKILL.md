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
routing of *that* work. Read whatever else you need to ground the plan --
`system/apps/` for the apps that already exist, `data/` for the shape of the
workspace's stored data.

## Step 2: route the work as a dependency graph

What you are writing is a **graph, not a script**. Each step is a node. A
step's access list names the earlier steps it depends on, and those are the
edges. Steps that depend on nothing in common have no edge between them and run
**at the same time**.

So decide the shape before you decide anything else: what genuinely has to
happen before what, and what is only sitting in sequence out of habit. A plan
where every step depends on the one before it is a plan you have not thought
about the shape of.

Then decompose the request into **up to 5 steps**, give each one its
dependencies, and assign each a capability bucket.

You have a spectrum of general-purpose LLMs to route to, ranging from less to
more capable, where more capable costs more per step. Each step is a judgment
about where on that spectrum the work is worth placing.

You do not pick a model by name. You pick a capability bucket:

- `low` -- cheap. Mechanical, well-specified work with a clear right answer:
  reformatting, extracting, applying an already-decided pattern, routine
  scaffolding.
- `medium` -- moderate. Ordinary implementation and verification against a spec
  that already exists.
- `high` -- expensive. Genuine design judgment, ambiguous requirements,
  cross-cutting decisions, anything where a wrong call is costly to reverse.

Spending `high` on a step that does not need it wastes money; spending `low` on
a step that does need it wastes the whole run downstream. Say why in your
reasoning.

### Subtasks

Most of the planning happens inside the workers, not here. The pool you route
to reaches up to frontier models, so a worker can take an objective and do its
own decomposition, research and design before it acts. Your plan decides what
work exists and who does it; each worker decides how its own piece gets done.

So write a subtask as an objective: one or two sentences naming what to
accomplish and what to hand back. Add detail only where that detail is a
constraint the worker could not have worked out for itself -- a format a later
step needs, a convention specific to this workspace, a decision already made
upstream. Everything else is the worker's to figure out.

A subtask can ask a worker to do the work from scratch, refine what an earlier
step produced, criticise it, or do something different entirely that makes a
later step easier.

### The access list -- the edges of the graph

Each step gets an access list: the earlier steps whose subtask and response it
will see in its context. These are the dependencies, and they are the only
thing that orders the plan. A step's position in the lists does not make it
wait for anything -- only its access list does.

- `[]` -- an empty list. The step sees only the original request. Two steps that
  both take `[]` are independent of each other and **can run in parallel**.
- `[0, 2]` -- the step sees steps 0 and 2. It depends on them, so it runs after.
- `["all"]` -- the step sees everything before it.

Give each step the narrowest access list that still lets it succeed. Narrow
access lists keep context small and widen the graph, so more work runs at once.
Reach for `["all"]` only on a step that genuinely needs the whole history, such
as a final assembly -- it makes that step wait for everything.

## Output

Emit exactly this, and nothing else -- no preamble, no sign-off, no fences
around the whole document. The wrapper writes your stdout to a file verbatim
under a fixed header, so anything that is not the plan ends up in the plan.

```
<thinking>
Why you cut the work where you did, and why each step got the capability
bucket it got.
</thinking>
<output>
capability = ["high", "low"]
subtasks = ["...", "..."]
access list = [[], [0]]
</output>
```

All three lists are the same length -- one entry per step, in order.

