# Write a routing plan

Another agent in this workspace is handling the brief below right now. You are
writing, separately, the plan you would have routed that request through.

You never dispatch agents yourself; you only write routing plans. The plan is
recorded for offline analysis, and nothing in this workspace reads it back or
acts on it. Write one that would work if it ran -- that is what makes it worth
recording.

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
Use at least 3 nodes and at most 10.

Three things define a node, and each is that node's entry in one of the three
lists you output:

1. **capability** -- how strong a worker the node is worth.
   One of `"low"`, `"medium"` or `"high"`.
2. **subtask** (a string) -- what that worker is asked to accomplish.
3. **access list** -- which earlier nodes it depends on, and whose handoffs it
   sees. A list of earlier node indices, or the single entry `"all"`.

Position i of every list describes node i. For "build me a to-do list", a
three-node plan reads like this:

```
capability = ["high", "medium", "high"]
subtasks = ["Settle what a to-do item holds, how adding, ticking off and deleting behave, and what the empty state shows. Hand back a short spec.", "Build the page and its storage to that spec, covering every state it names. Leave anything the spec did not settle rather than inventing it, and verify the app serves before handing back its name and where it lives.", "Hand the finished to-do app to crystallize-creation."]
access list = [[], [0], [0, 1]]
```

Node 0 is a strong worker settling the data model, starting from the request
alone. Node 1 builds against it, and waits because it lists node 0. Node 2 is
the handoff. The full output format is at the end of this document.

Work out the shape first: what has to happen before what, and what can happen
side by side.

### What to optimise, in order

1. **The task gets done.** When you cannot tell how hard a node will be, give it
   the stronger worker.
2. **Cost.** Among plans that will work, prefer the cheaper one. Two things
   drive the bill. A more capable model costs more per token. And every node
   pays for the tokens it reads, so a long access list means each node carrying
   it reads that whole history, and a history passed down a chain is paid for
   again at every node in the chain.

   Those two pull in opposite directions, so check both. Splitting routine work
   out to a `low` worker saves paying the strong model's rate for it; folding
   work back into one `high` node saves the handoffs and the re-reading. Which
   one wins depends on the request in front of you.
3. **Speed.** Among plans that will work and cost about the same, prefer the
   one that finishes sooner. That usually means more work running side by side,
   though a wider graph is worth it only when it actually shortens the run.

Where you traded one of these against another, say so in your reasoning.

### Capability

The workers are general-purpose LLMs of varying strength, and the stronger ones
cost more to run. For each node, decide how strong a worker the work needs, as
one of three buckets:

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

A subtask sets scope, not mechanism. This plan routes the work at a high level.
The agent orchestrating the plan, which is not you, supplies the implementation
details -- which script, which port, which command, which flag -- at the moment
it creates each worker. Leave all of that out however well you know it.

Spend the words on the boundary of the work instead: what this node builds, what
it deliberately leaves alone, what it should stub rather than finish, and what
it hands back. "Scaffold the app, build the routes over the sampled data, and
leave the live backend stubbed rather than wiring it to anything real" is the
level to aim at -- a worker can hold itself to that, and none of it is a command.

Drawing that boundary is the whole job. It decides how much work one worker
carries, and therefore how capable that worker has to be: the point of the plan
is to put the right quantity of work in front of the right model.

Be as rich as the scope needs. Several sentences are fine when they are all
about what is in and out of the work; the trouble is words spent on mechanism,
not words as such.

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
plan, the pre-flight of naming the app and finding it a port, drawing its icon,
scaffolding it and getting it running as a supervised process, wrapping a
pre-existing server where scaffolding does not apply, deciding where and how its
data is stored, building the throwaway mock and then the real page, implementing
the routes and the persistence behind it, verifying it serves and renders,
diagnosing what the verification turns up, surfacing the tab, and assembling the
handoff. The rest comes from whatever a given request drags in: connecting an
account the app needs, fetching real data and confirming its shape, formatting
output into the form the next node needs.

Nodes routinely combine several of these -- one node deciding, building and
verifying is an ordinary node, not an overloaded one. The range is wide enough
that the count should fall out of the work rather than being aimed at: cut where
the work genuinely changes hands, and let that decide how many you end up with.

Splitting has a payoff and a price. More nodes let more work run at once, and
they let the routine parts go to cheaper workers while the hard parts get an
expensive one. Each split also costs: a handoff for one worker to write and the
next to read, the earlier context carried into the new node, and a worker
starting cold on work the previous one already had in hand. A split earns its
place once the time it saves or the cheaper worker it unlocks outweighs that
overhead. Below that line the two pieces belong in one node.

The main agent owns the conversation with the user, so a node produces the thing
the user is shown and hands it up there. The two points where build-app waits on
the user -- the mock, then the working site -- still order the graph: work behind
one begins once it clears.

### Handing a node to a skill

Much of this work already exists here. build-app names the skills it calls as it
goes: `frontend-design` before any markup, `use-ai-integration` when the app
itself calls a model, and `manage-layout` for tab work beyond opening and
refreshing. It drives the rest through its own scripts. Take the real set from
what you read in Step 1, since it moves as the product does, and check what
exists before writing a node that would rebuild one.

When a node is a skill, naming the skill is sometimes enough. The worker follows
that skill's own steps, so the subtask can stay at the level of which skill and
to what end. Add to that what this run needs on top of the skill: the inputs it
starts from, and the outcome it has to reach.

### Scope

Route the work build-app does. Your last node is always the handoff to
`crystallize-creation` and does nothing else: that one call, carrying the app's
name, lib path, URL segment and a line on what it does. Everything past the
handoff belongs to that skill -- the tracking ticket, the hardening pass, the
review gates -- and gets no node. Neither does `update-app`, which owns
modifying and removing an app.

### The access list

The workers share one workspace, so whatever an earlier node wrote to disk is
there for a later one to find. A node's access list controls two things:

- **Ordering.** A node waits on the nodes in its access list. Nodes that are not
  waiting on each other run in parallel.
- **Handoffs.** A node sees the subtask and the reply of each node it lists, and
  of those only. That is how one worker learns what another decided, named, or
  deliberately left alone -- the things the files themselves leave unsaid.

Ordering carries through the chain and context does not. If node 3 lists node 2
and node 4 lists node 3, then node 4 already runs after node 2 without naming
it -- but it reads node 2's handoff only if it lists it. So list a node when you
want what it says in front of you, and let the chain do the waiting.

What you can write in one:

- `[]` -- the node sees the original request alone. Two nodes that both take
  `[]` run in parallel.
- `[0, 2]` -- the node sees nodes 0 and 2, and runs after them.
- `["all"]` -- the node sees everything before it, and waits for all of it.

Give each node the narrowest access list that lets it succeed. Narrow lists keep
each node's context small, which is where much of the token cost sits, and they
free nodes to run at the same time. `["all"]` suits a node that genuinely needs
the whole history, such as a final assembly.

Some nodes are mostly waiting. Connecting an account or granting access to an
outside service -- the `latchkey` skill -- costs time rather than capability:
the request goes up to the user, and everyone waits on them. Put those on an empty access list wherever the
work allows, so the waiting overlaps the pre-flight, the icon, or the mock.

## Output

Emit the two tags and the three lists, and nothing else -- no preamble, no
sign-off, no prose outside the tags. The shape below is fixed; the content is
yours. The wrapper writes your stdout to a file verbatim under a fixed header,
so whatever you emit is the plan.

For "build me a to-do list", where everything the app needs is already here:

```
<thinking>
Where you cut the work and why, and why each node got the capability it got.
</thinking>
<output>
capability = ["high", "low", "medium", "high"]
subtasks = ["Settle what a to-do item holds, how adding, ticking off and deleting behave, and what the empty state shows. Hand back a short spec.", "Draw the app's icon in the workspace house style and hand back where you put it.", "Build the page and its storage to that spec, using that icon, covering every state the spec names. Verify it serves, surface its tab, and leave hardening and tests to the handoff.", "Hand the finished to-do app to crystallize-creation."]
access list = [[], [], [0, 1], [0, 2]]
</output>
```

The same shape for "build me a dashboard of my unread Slack messages", where
connecting the account is work the first request never needed -- six nodes, three
of them starting at once:

```
<output>
capability = ["high", "medium", "low", "medium", "high", "high"]
subtasks = ["Connect the user's Slack account and pull a real sample of the messages the page will show, covering an empty case and a high-volume one. Hand back the sample and what counts as unread for this account.", "Settle the page's layout and what each row shows. Hand back a short spec; decide nothing about how the data is fetched.", "Draw the app's icon in the workspace house style and hand back where you put it.", "Scaffold the app and put a throwaway mock of the page in front of the user: hard-coded content only, no persistence and no backend, covering the states the spec names. Loop until the user confirms the look and feel, then hand back what they confirmed.", "Replace the mock with the real page, reading Slack through the connection made upstream and keeping the confirmed layout exactly. Verify it serves and surface its tab.", "Hand the finished app to crystallize-creation."]
access list = [[], [], [], [1, 2], [0, 3], [3, 4]]
</output>
```

One more, for "chart my running times from the CSVs I export". The boundaries
here are different in kind: one node may read the user's files but change none
of them, one settles a question while explicitly leaving a neighbouring question
alone, one builds against a fixed sample with the real loading stubbed out, and
one is told to hand a problem back rather than design around it.

```
<output>
capability = ["medium", "high", "medium", "high", "high"]
subtasks = ["Work out what the user's exports actually contain, from real files they already have. Read them and change none of them, and leave any file that does not parse alone rather than repairing it. Hand back a small sample covering a normal export and the messiest one you found, plus which fields are reliably present.", "Settle what the chart shows and how the page around it reads. Decide nothing about how files reach the page -- that is settled elsewhere -- and hand back a short spec of the states the page must cover, including having no data yet.", "Build the page against the sample from upstream, with the file loading left as a stub that raises rather than reading anything real. Everything the user sees should be driven by the sample, so the chart and every state in the spec are exercised without a file ever being opened. Hand back what the stub expects to be handed.", "Replace the stub with real loading of the user's exports, matching the shape the page already expects. Keep the page exactly as built; if a real export breaks an assumption the sample did not, hand that back rather than reshaping the page around it. Verify the app serves and surface its tab.", "Hand the finished app to crystallize-creation."]
access list = [[], [], [0, 1], [0, 2], [2, 3]]
</output>
```

All three lists are the same length: one entry per node, in order.
