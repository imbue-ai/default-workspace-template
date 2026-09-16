# Write a build plan

An orchestrating agent in this workspace is building an app for the user. It has
briefed you below, and it will carry out the plan you write as soon as you
finish: it launches a worker for each node, waits for their reports, runs the
user conversations itself, and hands the finished app to hardening. The user
never sees your plan.

You have read-only tools and a single turn, so work from what you can read.

## Step 1: read the work you are planning

Start with `.agents/skills/build-app/SKILL.md`. It describes how an app is built
here: the checks with the user, the scaffolder, the throwaway mock, the real
routes, verification, surfacing the tab. Your plan divides that work among
workers. Parts of it do not become nodes, because the orchestrator owns them:
clarifying questions, launching the hardening pass, and anything in Step 5.

Then read `.agents/skills/build-app-parallel/references/worker-node.md`. It is
what every worker follows, so it tells you what a worker can and cannot do.

Then read enough of the workspace to ground the plan:

- `.agents/skills/` -- the skills a node can be handed to. Skim each `name` and
  `description`, and open the ones this request plausibly touches.
- `system/services/` -- the background services already running.
- `system/apps/` -- the apps that already exist.
- `data/` -- the shape of the workspace's stored data.

## Step 2: write the plan

The plan is a DAG of nodes. Each node is one piece of the work assigned to one
worker, or one conversation the orchestrator has with the user, and the edges
are what each node depends on. Use at least 3 nodes and at most 15.

Three things define a node, and each is that node's entry in one of the three
lists you output:

1. **capability** -- how strong a worker the node is worth. One of `"low"`,
   `"medium"` or `"high"`, or `"interactive"` for a conversation with the user
   that the orchestrator runs itself.
2. **subtask** (a string) -- what that worker is asked to accomplish, or for an
   interactive node, what the orchestrator shows the user and asks.
3. **access list** -- which earlier nodes it depends on, and whose handoffs it
   sees. A list of earlier node indices, or the single entry `"all"`.

Position i of every list describes node i. The full output format, with
examples, is at the end of this document.

Work out the shape first: what has to happen before what, and what can happen
side by side.

### What to optimise, in order

1. **The task gets done.** When you cannot tell how hard a node will be, give it
   the stronger worker.
2. **Speed.** The user is waiting. Among plans that will work, prefer the one
   that puts something in front of the user sooner and finishes sooner. That
   usually means more work running side by side, within the limits below.
3. **Cost.** Every node pays for the tokens it reads, and a long access list
   means each node carrying it reads that whole history. Among plans that are
   about as fast, prefer the cheaper one.

Where you traded one of these against another, say so in your reasoning.

### Capability

- `low` -- mechanical, well-specified work with a clear right answer:
  reformatting, extracting, applying a decided pattern, routine scaffolding.
- `medium` -- ordinary implementation and verification against a spec that
  already exists.
- `high` -- genuine design judgment, ambiguous requirements, cross-cutting
  decisions, anything a wrong call is expensive to reverse.
- `interactive` -- a conversation with the user, run by the orchestrator. No
  worker, no tokens.

Under-buying on a node that needed the capability costs that node and
everything downstream of it, so spend where the risk is.

### How the workers run

The brief names the app. Every node uses that name; no node picks another.

Every worker runs in the same folder: one git checkout the orchestrator created
for this build. Whatever an earlier node wrote to disk is there for a later node
to find the moment it is written. Up to 5 workers run at once. Workers do not
commit; the orchestrator commits when none is running.

A shared folder has one hard rule: **two nodes that run at the same time must
never edit the same file.** Nothing detects it; the later write silently wins.
So:

- **Give each node a boundary in files.** Say in the subtask what it owns: the
  page's template and static files, the storage module, the routes module. A
  node that runs alongside others touches nothing outside that.
- **One node owns the shared registration files.** Scaffolding the app edits the
  root `pyproject.toml`, `uv.lock` and `system/supervisord.conf`, and picks a
  port. Exactly one node scaffolds. Any library the app will need is added by
  that node, or later by a node that runs with no other node beside it, because
  adding a dependency rewrites those shared files too.
- **Split files where work would otherwise collide.** If two pieces would both
  edit the runner, give one of them its own module and let a later node wire it
  in. Name that wiring in both subtasks.

Nodes do only a quick check of their own piece: it runs, it serves, it renders.
No node writes a thorough test suite or runs review; one hardening pass covers
the whole app after your plan ends.

### Subtasks

A subtask sets scope, not mechanism. Workers are general-purpose models that do
their own decomposition and design, and they read `build-app` for the
mechanics. Leave out which script, which port, which command, which flag.

Spend the words on the boundary of the work instead: what this node builds,
which files it owns, what it deliberately leaves alone, what it should stub
rather than finish, and what it hands back. "Build the storage module and the
routes over it, leave the page alone, and hand back each route and what it
returns" is the level to aim at.

Where two pieces would otherwise queue, look for the contract between them and
let both start: one builds against a stub or a fixed sample, the other builds
the real thing behind the same shape, and a later node swaps them. Name that
contract in both subtasks, so the swap is a rewire rather than a rebuild.

Split a decision out when more than one later node needs it. A node that settles
the record shape and then builds the data layer makes everyone waiting on that
shape wait for the data layer as well.

Splitting has a price: a handoff for one worker to write and the next to read,
and a worker starting cold on work the previous one already had in hand. A split
earns its place once the time it saves or the cheaper worker it unlocks outweighs
that. Below that line the two pieces belong in one node.

Nodes take many shapes, and which ones appear follows from the request: settling
the defaults, drawing the icon, scaffolding, deciding where data is stored,
building the throwaway mock and then the real page, implementing routes and
persistence, verifying it serves, connecting an account the app needs, fetching
real data and confirming its shape. Let the request decide.

### The user conversations

A build has exactly two conversations with the user: the mock, then the working
site. Each gets its own `interactive` node. Its subtask says what to show and
what to ask; its access list is the node that built what is shown. The
orchestrator shows the user a preview served from the build folder, and relays
any change the user asks for to the worker that built it, which stays running
until the user confirms. So plan no separate revision node: the confirmation is
what the interactive node hands back.

No other node talks to the user. Where a node meets something it would rather
ask about -- a name, a default, an ambiguity in the brief -- it decides, and
hands the decision back with its work.

A node that does not list an `interactive` node runs while the user is still
deciding. Check whether it needs the answer or only what was settled before the
question. How long that wait runs is unknowable, which is why it is worth putting
whatever can run there alongside it.

Some nodes are mostly waiting on the user too: connecting an account or granting
access to an outside service (the `latchkey` skill). Put those on an empty access
list wherever the work allows, so the waiting overlaps other work.

### Handing a node to a skill

Much of this work already exists here. `build-app` names the skills it calls as
it goes: `frontend-design` before any markup, `use-ai-integration` when the app
itself calls a model, and `manage-layout` for tab work beyond opening and
refreshing. When a node is a skill, naming the skill and the outcome it has to
reach is often enough.

### Where the plan ends

Your last node is the working-site conversation. After it, the orchestrator
merges the build and hands the app to hardening itself, so plan no handoff node,
no hardening node and no test-suite node. Neither does `update-app` get a node;
it owns modifying and removing an app after it exists.

### The access list

A node's access list controls two things:

- **Ordering.** A node waits on the nodes in its access list. Nodes that are not
  waiting on each other run at the same time.
- **Handoffs.** A node sees the subtask and the report of each node it lists,
  and of those only. That is how one worker learns what another decided, named,
  or deliberately left alone -- the things the files themselves leave unsaid.

Ordering carries through the chain and context does not. If node 3 lists node 2
and node 4 lists node 3, node 4 already runs after node 2 -- but it reads node
2's report only if it lists it.

What you can write in one:

- `[]` -- the node sees the brief alone. Two nodes that both take `[]` start
  together.
- `[0, 2]` -- the node sees nodes 0 and 2, and runs after them.
- `["all"]` -- the node sees everything before it, and waits for all of it.

Give each node the narrowest access list that lets it succeed.

## Output

Emit the two tags and the three lists, and nothing else -- no preamble, no
sign-off, no prose outside the tags. Each list is valid JSON on one line, with
double-quoted strings. The orchestrator parses your output mechanically: a plan
whose lists differ in length, or whose access lists point at later nodes, is
rejected.

For "build me a to-do list", where everything the app needs is already here:

```
<thinking>
Where you cut the work, which files each node owns, and why each node got the
capability it got.
</thinking>
<output>
capability = ["high", "medium", "medium", "interactive", "medium", "high", "interactive"]
subtasks = ["Settle what a to-do item holds, how adding, ticking off and deleting behave, and what the empty state shows. Write no code. Hand back a short spec and the JSON contract the page reads through.", "Pre-flight and scaffold the app, serving its placeholder page, with every library the app will need. Build no routes and no UI beyond scaffolding. Hand back the app name, lib path, package folder and port.", "Build a throwaway mock of the page to that spec, in the page template and static files only: hard-coded content covering every state the spec names. Hand back what it shows and which files it owns.", "Show the user the mock and ask whether the look and feel is right. Bring back what they confirmed.", "Build the storage module and the routes over it to that spec, behind the JSON contract, in new modules of their own; leave the page template and static files alone. Hand back each route and what it returns.", "Replace the mock's hard-coded content with calls to those routes, keeping the confirmed look exactly, and wire the new modules into the runner. Verify the app serves and renders. Hand back what changed.", "Show the user the working site and ask whether it does what they wanted. Bring back their answer."]
access list = [[], [], [0, 1], [2], [0, 1], [3, 4], [5]]
</output>
```

Nodes 0 and 1 start together. Node 4 runs while the user looks at the mock,
because it needs only the spec and the scaffold, and it owns different files
from the mock.

The same shape for "build me a dashboard of my unread Slack messages", where
connecting the account is work the to-do list never needed -- three nodes start
at once, and the account connection waits on the user alongside them:

```
<output>
capability = ["interactive", "high", "low", "medium", "medium", "interactive", "high", "high", "interactive"]
subtasks = ["Ask the user to connect their Slack account for this app, using the latchkey skill. Bring back whether access was granted.", "Settle the record shape, what a refresh does to what is already stored, and the JSON contract the page reads through. Decide nothing about layout. Hand back a short spec and that contract.", "Draw the app's icon in the workspace house style and hand back where you put it.", "Pre-flight and scaffold the app with that icon, with the Slack client and every other library the app will need. Build no routes, no data layer and no UI beyond scaffolding. Hand back the app name, lib path, package folder and port.", "Build a throwaway mock of the page to the spec in the page template and static files only, with representative fake messages including an empty inbox and an overflowing one. Hand back what it shows.", "Show the user the mock and ask whether the look and feel is right. Bring back what they confirmed.", "Build the Slack fetch and the storage module to the spec, in modules of their own, reading real unread messages through the connected account. Leave the page and the runner alone. Hand back what one refresh stores and how long it takes.", "Add the routes over that storage behind the JSON contract, wire the new modules into the runner, and replace the mock's content with calls to those routes, keeping the confirmed look. Verify the app serves and renders real messages. Hand back what changed.", "Show the user the working site and ask whether it does what they wanted. Bring back their answer."]
access list = [[], [], [], [1, 2], [1, 3], [4], [0, 1, 3], [5, 6], [7]]
</output>
```

All three lists are the same length: one entry per node, in order.
