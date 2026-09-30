---
name: build-app
description: "Use when you want to create a new app for the user -- a page, dashboard, or tool they can open as a window on the desktop. A planner splits the build into parts, workers build those parts side by side in a git worktree each, and you merge their work, handle every contact with the user (including the throwaway mock and working-site reviews) and take the confirmed app live. For changing or removing an existing app use update-app."
metadata:
  author: imbue
---

# Building an app

You orchestrate. You do not build the app yourself:

- **A planner** reads `references/app-building-guidance.md` and the workspace,
  and writes a plan: a small
  graph of nodes, each one piece of the build, with what each depends on. It is
  a headless, read-only Claude run on this flow's own prompt,
  `references/planner-prompt.md`, started by `scripts/run_planner.sh`.
- **Workers** build the nodes, several at once, each in a git worktree of its
  own, branched off the build branch as it stands when the worker starts. Each
  follows `references/worker-node.md`, commits its piece and reports back.
- **You** clarify the request, run the planner, launch workers as their
  dependencies finish, **merge each finished node into the build branch** so the
  nodes after it start from it, run the interactive nodes (every contact with the
  user), and hand the app to hardening.

Every node's work reaches the next one through the build branch: a worker starts
from its tip and hands back a branch, and you merge that branch the moment the
worker reports. A node launched before its dependency is merged would not see it
at all.

`references/app-building-guidance.md` is how an app is built here -- the
scaffolder, ports, the manifest, registration, verification, teardown. The
planner and the workers read it for the piece they are working on; you open it to
look something up. Do not work through it yourself: it describes building an app
end to end, and following it from the top is building the app by hand, which is
what this skill exists to replace. The steps below are yours.

**You speak to the user only when an interactive node says to.** The plan names
every conversation the build has, and those are the only messages you send: no
acknowledgement when the request arrives, no note that planning has started, no
progress while nodes run. If the user writes to you meanwhile, answer only what
they asked, in one line, and carry on.

**One turn carries the whole build.** From the moment you start planning until
an interactive node is due, you stay inside a single turn: you wait for each
worker with a foreground `await` (Step 4), read its report, launch whatever
became ready, and wait again. Ending the turn is how you hand the floor back to
the user, so every end mid-build invites a "how's it going?" that you must then
answer -- three runs were lost that way, with the conversation exhausted before
the app was live. The only turn ends in a build are the ones an interactive node
asks for, plus the final handoff.

`scripts/plan_orchestration.py` does the bookkeeping with a single right answer:
checking the plan, listing which nodes can start, and writing each worker's task.
Run it with bare `python3`.

Run every command below from the repo root, and never pipe one through `tail` or
`head` to shorten its output -- a pre-tool hook refuses that outright, and each
refusal costs a turn. Redirect to a file and read the file instead.

## Conventions

Pick the app's kebab-case name `$APP` up front (the rules are in `build-app`'s
pre-flight: DNS-safe, not starting with `host-` or `agent-`, and not a name an
app already registers). Workers may refine the display name, but `$APP`
names everything below.

| Thing | Value |
|---|---|
| Run folder (plan, tasks, reports) | `data/.tasks/build-app/$APP/` (call it `$RUN`) |
| Integration folder | `$HOME/worktrees/build-app-$APP` (call it `$BUILD`) |
| Build branch | `build-app/$APP`, checked out in `$BUILD` |
| Worker for node N | `$APP-node-N`, in a worktree mngr makes for it |
| Branch of node N | `mngr/$APP-node-N`, branched from the build branch |
| Progress record | `$RUN/progress.txt`, two lines: `done: <indices>` and `running: <indices>` |

Keep `$RUN/progress.txt` current after every launch, report and conversation. It
is how you resume if your context is compacted mid-build.

## Progress timeline

Everything the user reads here -- stage titles, summaries, the questions at each
review -- follows `.agents/shared/references/user-facing-language.md`. Plans,
nodes, workers, folders, merges and commits are machinery the user never hears
about.

Show the user stages, not nodes. Create these steps up front, in order -- that
costs nothing, since no worker exists yet -- and close each when its stage ends.
Once workers are running, a stage transition is bookkeeping like any other: do it
*after* you have launched everything that is ready, never between two launches.

1. "Understand what you want"
2. "Plan the build"
3. "Build a first version to show you"
4. "Show you the first version"
5. "Build the working app"
6. "Show you the working app"
7. "Take the app live and start the thorough checks"

Workers keep running in the background across stage boundaries (a node that
does not need the mock review runs while the user looks at the mock). That is
expected; the stage reflects what the user is waiting on.

This timeline is the only one there is. Workers keep no records of their own, so
nothing they do appears here except through the stage you are in and the report
you read when they finish.

## Step 1: Clarify (business terms only)

**Start the integration folder before you ask anything.** It does not depend on the
plan or on the answers, and its `uv sync` is the slowest thing in a build's opening --
slow enough that overlapping it with the planner alone has not been enough. Clarifying
is time the orchestrator otherwise spends waiting on the user, so spend it syncing.

Pick `$APP` from the request as it stands; a clarifying answer rarely changes it, and
`git branch -m` renames the branch if one does. Commit any pending changes in the main
checkout first (commit, never stash; the build branch starts from your last commit):

```bash
mkdir -p "$RUN"
git worktree add -b "build-app/$APP" "$BUILD" HEAD
(cd "$BUILD" && nohup uv sync --all-packages > "$RUN/sync.log" 2>&1 &)
```

**Then start the three workers, before the plan exists.** With
`settings.tier_agents` (Step 2) there is one agent per difficulty and the three are
always `low`, `medium` and `high`, on Haiku, Sonnet and Opus -- none of which the plan
decides. So they can go up now, and spend the clarifying and the planning doing the
reading every node would otherwise repeat: in one measured build a worker spent 144
seconds reading 8 files before it wrote anything, and its sibling spent 24 more.

`references/tier-agent-priming.md` is their first task: read `worker-node.md`,
`app-building-guidance.md`, `worker-reporting.md` and the scaffolder, report ready, and
wait. Give each its own runtime dir so their reports do not collide:

```bash
python3 .agents/skills/build-app/scripts/plan_orchestration.py models | while read -r NAME MODEL; do
    mkdir -p "$RUN/agents/$NAME/reports"
    cp .agents/skills/build-app/references/tier-agent-priming.md "$RUN/agents/$NAME/task.md"
    uv run .agents/skills/launch-task/scripts/create_worker.py launch \
        --name "$APP-$NAME" \
        --template shared_worker \
        --work-folder "$BUILD" \
        --runtime-dir "$RUN/agents/$NAME/" \
        --task-file "$RUN/agents/$NAME/task.md" \
        --create-arg=-S \
        --create-arg=agent_types.claude.settings_overrides.model="$MODEL" \
        --message-with-mngr
done
```

Do not wait for their ready reports. They read while you clarify, and Step 4 sends the
first node to an agent that is already warm -- `launch` there becomes a `reply`, since
every tier agent exists from here.

A plan that turns out to use only two difficulties leaves one agent idle, which costs a
create and is destroyed with the rest in Step 6.

Then ask only the questions that genuinely block: a fork that is both genuinely
uncertain and expensive to reverse. Most apps have none. Default to the simplest
conventional choice and to a single user, and state each default in one line.
Phrase any real blocker as its user-visible consequence ("should everyone see the
same list?"), never a technical term. Do not propose a plan or ask for approval:
the user never sees the plan, and the mock is their first checkpoint.

If `fetch-process-show` sent you here with a confirmed `sample.json`, note its
path; the mock must render it.

## Step 2: Plan

Write the brief the way you would brief a colleague picking this up: what the
user wants, every default you stated, the app name `$APP`, and the path of any
handed-off sample. Give context, not a plan; working out the plan is the
planner's job.

```bash
mkdir -p "$RUN"   # already there if Step 1 ran; harmless either way
cat > "$RUN/brief.md" <<'BRIEF'
<the brief>
BRIEF
```

The integration folder is already there and already syncing, from Step 1. It is where
every node's branch is merged and where the previews are served from, which is why it
needs a working checkout of its own. If you skipped that -- a build that did not come
through Step 1 -- make it now, before the planner, so the two run together:

```bash
git worktree add -b "build-app/$APP" "$BUILD" HEAD
(cd "$BUILD" && nohup uv sync --all-packages > "$RUN/sync.log" 2>&1 &)
```

Run the planner in the foreground, with the longest tool timeout you can
give it; waiting it out inside your turn keeps the floor. Send the user nothing
while it runs:

```bash
.agents/skills/build-app/scripts/run_planner.sh "$RUN"
```

When it exits 0, check the plan:

```bash
python3 .agents/skills/build-app/scripts/plan_orchestration.py parse --run-dir "$RUN" \
    --reduce-access --shared-worktree --tier-agents
```

Those last two are what decide the shape of the whole build, and `parse` prints which it
wrote. `--shared-worktree` runs every worker in `$BUILD` rather than cutting a worktree
each, so there is no per-worker `uv sync` and no branch to merge. `--tier-agents` keeps one
agent per capability alive across that capability's nodes, so a node after the first starts
from what the agent already learned instead of reading its way in from nothing. Step 4 has
the launch commands for both.

If `parse` exits 2, the message names what is wrong. Move `plan.md` aside to
`plan.rejected-1.md`, append one line to the brief quoting the problem ("Your
previous plan was rejected: <message>"), and run the planner once more. If the
second plan is also rejected, or the planner itself fails twice, stop and tell
the user the build could not be planned, pointing at `$RUN/log`. Do not
fall back to building the app yourself without asking.

Read `plan.json` yourself before starting. You will need each node's subtask to
know what its report should contain and which nodes build what the user reviews.

## Step 3: Finish the integration folder

The folder itself was made in Step 2, and its sync has been running since. Check
it landed before you serve any preview from it:

```bash
(cd "$BUILD" && uv sync --all-packages)
```

Running it again is how you wait for it: uv locks the environment, so this
blocks until the background sync lets go and then returns at once, having
nothing left to do. If it reports an error, `$RUN/sync.log` has what the first
one printed.

The workers do not wait on this. Each one gets a worktree of its own, and its
`uv sync` runs as part of its create, so a worker can start before this finishes.

Copy anything under `data/` the build needs (such as a handed-off `sample.json`)
into `$BUILD` at the same relative path; `data/` is not part of the checkout. A
worker that needs it gets it the same way, in its own worktree, once it exists.

A failed `mngr create` (for example on a Claude Code version mismatch) cleans up
by removing the worktree it made. Here that is the worker's own, empty, and
nothing else is at risk: `$BUILD` is yours, no create was given it, and every
finished node is a commit on the build branch. Relaunch the node.

## Step 4: Run the plan

Repeat until every node is done.

1. **Find what can start.**

   ```bash
   python3 .agents/skills/build-app/scripts/plan_orchestration.py ready \
       --run-dir "$RUN" --done <done indices> --running <running indices>
   ```

   It prints comma-separated node indices. It never starts more than 5 workers at
   once, and a node with `"has_worker": false` takes no worker slot.

2. **Check each printed node's `has_worker` in `$RUN/plan.json` before launching
   anything.** `false` means the node is yours: do its subtask in `$BUILD` yourself,
   commit it, and count it done -- no worker, no worktree, no merge. That is always
   true of an interactive node, and under `--only-parallel-workers` it is also true of
   a node nothing else runs beside, where a worker would cost a worktree, a sync, a
   cold start and a merge on a wait nobody overlaps.

   **Write a report for each node you do**, at `$RUN/nodes/N/reports/report.md`, exactly
   as an interactive node does (Step 5, item 3). A later node's task file quotes the report
   of every node in its access list, and `write-task` **fails** when one is missing -- so a
   node you did yourself and left unreported blocks every node that depends on it.

   **Do a whole run of your own nodes at once.** `plan.json`'s `own_groups` lists them
   already grouped: `[[0], [4, 5]]` means node 0 stands alone, and nodes 4 and 5 are one
   piece of work. When `ready` prints the first node of such a group, write all of it in
   one go -- one design, one set of files, one commit -- then write each node's report and
   count every node in the group done. Those nodes are consecutive and all yours, so keeping them apart divides
   the work between you and yourself, and each split invites another pass over the same
   files. A group ends only at a node that has a worker, because you must wait for it and
   merge its branch before building on it.

   An interactive node sits inside a group like any other -- it is you asking a question
   you then act on. It does fix an order within the group: ask it when you reach it, and
   write the nodes after it against the answer rather than designing past them.

3. **Launch each node whose `has_worker` is true, one node per command.** Never put
   two launches in one shell command: they run one after the other anyway, and a
   batched launch hides every worker after the first from the evidence an eval
   collects. Read the node's `model` and `agent` from `$RUN/plan.json`, and
   `plan.json`'s `settings` for which of the two shapes below this build runs.

   **`settings.shared_worktree` false and `settings.tier_agents` false** -- the default,
   a fresh worktree and a fresh agent per node:

   ```bash
   python3 .agents/skills/build-app/scripts/plan_orchestration.py write-task \
       --run-dir "$RUN" --node N
   uv run .agents/skills/launch-task/scripts/create_worker.py launch \
       --name "$APP-node-N" \
       --template worktree_worker \
       --branch "build-app/$APP:mngr/$APP-node-N" \
       --runtime-dir "$RUN/nodes/N/" \
       --task-file "$RUN/nodes/N/task.md" \
       --create-arg=-S \
       --create-arg=agent_types.claude.settings_overrides.model=<model> \
       --message-with-mngr
   ```

   **`settings.shared_worktree` true** -- every worker runs in `$BUILD`, which you made and
   synced in Step 2, so there is no worktree to cut and no sync to pay. Use the
   `shared_worker` template and `--work-folder "$BUILD"`, and drop `--branch`: nobody
   commits per node here, so there is nothing to merge either, and **item 6's merge does not
   apply** -- a node is done when its report lands.

   ```bash
   uv run .agents/skills/launch-task/scripts/create_worker.py launch \
       --name "$APP-<agent>" \
       --template shared_worker \
       --work-folder "$BUILD" \
       --runtime-dir "$RUN/nodes/N/" \
       --task-file "$RUN/nodes/N/task.md" \
       --create-arg=-S \
       --create-arg=agent_types.claude.settings_overrides.model=<model> \
       --message-with-mngr
   ```

   **`settings.tier_agents` true** -- the node's `agent` is a capability (`low`, `medium`,
   `high`), not a node number, and several nodes name the same one. **All three exist already**, started
   in Step 1 and primed with the reading every node would otherwise repeat, so every node
   is a message to an agent that is warm -- there is nothing to launch here:

   ```bash
   python3 .agents/skills/build-app/scripts/plan_orchestration.py write-task \
       --run-dir "$RUN" --node N
   # `launch` syncs the node folder into the worker for you; `reply` sends text and nothing
   # else, so put the folder where the task file says it is before sending it. Without this
   # the worker is told to write its report into a directory that is not there.
   mkdir -p "$BUILD/$RUN/nodes/N/reports"
   cp "$RUN/nodes/N/task.md" "$BUILD/$RUN/nodes/N/task.md"
   uv run .agents/skills/launch-task/scripts/create_worker.py reply \
       --name "$APP-<agent>" \
       --task-file "$RUN/nodes/N/task.md" \
       --message-file "$RUN/nodes/N/task.md" \
       --message-with-mngr
   ```

   The task file is named twice on purpose: `--message-file` is what the agent receives, and
   `--task-file` is the required argument `reply` normally reads a worker id out of, which
   `--message-with-mngr` addresses by name instead.

   **Read the report from `$BUILD`, not from `$RUN`.** A tier agent writes to the copy you
   made, so `await` has to look there:

   ```bash
   uv run .agents/skills/launch-task/scripts/create_worker.py await \
       --name "$APP-<agent>" --task-file "$BUILD/$RUN/nodes/N/task.md" --timeout 9m
   # `await` archives the report under the copy it read. Put it back where `write-task`
   # looks, or the next node that depends on this one cannot have its task written at all.
   mkdir -p "$RUN/nodes/N/reports/consumed"
   cp "$BUILD/$RUN/nodes/N/reports/consumed/"* "$RUN/nodes/N/reports/consumed/"
   ```

   One agent does its nodes **one at a time**, so two nodes of the same capability never run
   at once; nodes of different capabilities still do. Send a node only once every node in its
   access list is done, exactly as `ready` says.

   Launch every ready node before you wait for any of them -- that is what makes
   them run at once -- and put the bookkeeping after the launches, not between
   them. Updating `$RUN/progress.txt`, destroying a finished worker, reading a
   report, **closing one stage and starting the next with `tk`**: every one of
   those is time no worker is being started, and every one of them runs just as
   well once the workers are going. The order that keeps the build busy is
   **launch, launch, launch, then tidy up while they work**.

   The one thing that cannot wait is the merge. When a report lands mid-wave,
   merge that node's branch (item 6) *before* you run `ready`, because a node
   launched off an unmerged build branch cannot see the work it depends on.
   Merge, launch whatever that unblocked, then record and destroy while the new
   one runs.

   What these options are for:

   - `--branch <build branch>:mngr/$APP-node-N` is the one that matters. It
     branches the worker's worktree off the build branch **as it stands right
     now**, which is how the node sees everything merged before it, and it names
     the branch you will merge back. Without it mngr branches from your own
     checkout's HEAD, and the node would start from a workspace where none of the
     build has happened. Write both halves: the `BASE:NEW` form cuts a new branch
     from the build branch without checking it out, which is what lets this work
     while `$BUILD` has that same branch checked out.

   - `--message-with-mngr` sends the task with `mngr message`, and you pass it to
     `reply` too. These workers are not chats anyone opens, and the chat app's
     send route knows an agent only once it has re-read mngr's agent list, so a
     worker messaged seconds after its create can fall into the window where it
     answers 404 -- and its success answer means "delivered or queued" either
     way. That window is where a task went missing and left two workers idle.
   - The `-S` pair sets the model for this one worker, and the value is the node's
     `model` in `plan.json` -- chosen by the capability the planner gave it, so a
     `high` node runs on Opus, a `medium` one on Sonnet and a `low` one on Haiku
     rather than every node costing Opus. `MODEL_BY_CAPABILITY` in
     `scripts/plan_orchestration.py` is the one place that mapping lives; edit it to
     put a whole build on one model. Write each `--create-arg` joined with `=`, or an
     argument starting with `-` is read as an option of `launch` itself.

4. **Start each interactive node it printed** with Step 5. Add it to `running`.

5. **Wait for the running workers, in the foreground, one at a time.** Take them
   in the order you launched them:

   ```bash
   uv run .agents/skills/launch-task/scripts/create_worker.py await \
       --name "$APP-node-N" --task-file "$RUN/nodes/N/task.md" --timeout 9m
   ```

   Run it as an ordinary foreground command with the longest tool timeout you
   can give it, and keep your turn open. The wait is a poll on the worker's
   report, not a sleep: the other workers keep building throughout, and a node
   that finished while you were waiting on an earlier one returns immediately
   when its turn comes.

   `--timeout 9m` is short on purpose -- it fits inside one tool call. **Exit 124
   means only that the 9 minutes elapsed**, so re-run the same command; a worker
   that takes half an hour just takes several of these. Give up on a node only
   when the worker itself is gone (`stuck`, below).

   Do not put this wait in the background, and do not end your turn to wait it
   out. Both hand the floor back to the user in the middle of a build. This is
   the one place a build departs from `lead-proxy.md`, whose "Never sleep on a
   worker" arms the poll in the background and ends the turn: that keeps a chat
   answerable between short delegations, while a build is many workers deep and
   an hour long, and each turn it ends costs it a conversation.

6. **Handle each report as its `await` returns.** Follow
   `.agents/shared/references/lead-proxy.md` for reading the report, diagnosing
   a timeout, and a worker stopped for memory (exit 75) -- everything but its
   polling advice, which item 5 replaces. The reports dir is
   `$RUN/nodes/N/reports/`.
   - **`done`:** leave the report where it is -- later nodes' task files quote
     it. **Merge the node's branch before anything else**, because until you do,
     a node launched after it starts from a build branch without its work:

     ```bash
     git -C "$BUILD" merge --no-ff "mngr/$APP-node-N" -m "$APP node N"
     ```

     A conflict here means two nodes that ran side by side wrote the same file,
     which the plan is supposed to prevent -- see "When things go wrong". Resolve
     it in `$BUILD` keeping both sides' work, or, if the two are genuinely
     incompatible, stop launching and follow that entry.

     Then move N from `running` to `done`. If a later interactive node reviews
     what this node built, keep the worker running so it can apply the user's
     changes -- and merge again after each change it reports. Otherwise destroy
     it:
     `uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name "$APP-node-N"`.
     Destroying a worker removes its worktree; the branch you merged stays.
   - **Exit 76 (the worker's agent is not running and no report arrived).** A
     worker that has not yet started its first turn reads the same way as one
     that stopped early, and the check gives up after about fifteen seconds, so
     read this as "not working *yet*", not "broken". Look for a delivered report
     first (`$RUN/nodes/N/reports/`), and if there is none, nudge it once:

     ```bash
     uv run .agents/skills/launch-task/scripts/create_worker.py reply \
         --task-file "$RUN/nodes/N/task.md" \
         --name "$APP-node-N" --message-with-mngr \
         -m "Start your subtask now and report when it is done."
     ```

     Then wait on it again. Treat a second 76 on the same node as `stuck` below.
     Never destroy and relaunch a worker to restart it: you get two agents under
     one name, the second inherits none of the first's work, and the evidence an
     eval collects for that node becomes unreadable.
   - **`stuck`:** stop the worker
     (`uv run .agents/skills/launch-task/scripts/create_worker.py stop --name "$APP-node-N"`),
     which keeps its work for inspection, stop launching new nodes, let running
     workers finish, and follow
     `.agents/skills/launch-task/references/worker-failure.md`: tell the user
     what the node could not do, in plain terms, and ask how to proceed. Do not
     retry silently.

7. **Nothing to commit.** Each worker commits its own piece, and item 6 merges
   it, so the build branch already holds every finished node. Check that is true
   before a review or the final merge -- `git -C "$BUILD" log --oneline` should
   name every node in `done`, and `git -C "$BUILD" status --porcelain` should be
   empty. A node whose work is missing there is a worker that reported without
   committing: message it to commit, and merge again.

## Step 5: The interactive nodes

Every contact with the user during the build is an interactive node, and you run
each one yourself by doing what its subtask says. Workers never ask the user
anything. There are two kinds:

- **Reviewing what was built** -- the mock, then the working site. The node's
  access list names the node that built what is shown, and that node's report
  names the app, its package folder and anything the preview needs. Follow items
  1 to 4 below.
- **Anything else the user has to do**, such as connecting an account or granting
  access through the `latchkey` skill. Do it in this chat by following that
  skill, then record the outcome as the node's report (item 3 below).

For a review:

1. **Serve a preview from the integration folder.** It holds every node merged
   so far, which is what the user is being shown; a node whose branch you have
   not merged is not in it. The app is not live yet, so show a throwaway instance
   wrapped in a labeled preview window, with its own scratch data folder:

   ```bash
   python3 .agents/shared/scripts/serve_isolated_instance.py up \
       --name "$APP-preview" --cwd "$BUILD" \
       --port-env <PACKAGE_UPPER>_PORT \
       --env <PACKAGE_UPPER>_DATA_DIR="$RUN/preview-data" \
       --service-name "$APP-preview-app" \
       --preview-service-name "$APP-preview" \
       --preview-title "<App name> (preview)" \
       -- uv run "$APP"
   python3 system/scripts/layout.py open "$APP-preview"
   ```

2. **Ask the node's question** in business terms, and loop until the user
   **explicitly confirms**. For each change they ask for:
   1. Send the change to the builder, in the user's voice:

      ```bash
      uv run .agents/skills/launch-task/scripts/create_worker.py reply \
          --task-file "$RUN/nodes/K/task.md" \
          --name "$APP-node-K" --message-with-mngr -m "<the change>"
      ```

      Then wait on it again (Step 4, item 5). The `await` that printed the
      builder's last report already archived it, so its next report lands
      cleanly.
   2. When its new report lands, **merge its branch again** -- the change is a
      new commit on the same branch, and the preview serves `$BUILD`:

      ```bash
      git -C "$BUILD" merge --no-ff "mngr/$APP-node-K" -m "$APP node K: revision"
      ```

      Then run `python3 system/scripts/layout.py refresh "$APP-preview"` and show
      the user the change visibly applied.

   Nodes that do not depend on this conversation keep running meanwhile. If the
   user's answer changes something a running or finished node built against,
   message that node's worker with the change too, or tell the user it will be
   picked up in the next stage.

3. **Record the answer as this node's report**, so the nodes after it read it:
   write `$RUN/nodes/N/reports/report.md` with what the user confirmed and every
   change they asked for (or, for an account connection, what access was granted).
   Move N to `done`.

4. **Tear down the preview:**
   `python3 .agents/shared/scripts/serve_isolated_instance.py down --name "$APP-preview"`.

For the working-site conversation, use the same signals as `build-app` Step 5:
change requests are cheap iterations that reset the clock, cosmetic tweaks mean
the core is settled (ask "seems like we've got the core thing settled -- good to
lock it in?"), and only an explicit confirmation ends it.

## Step 6: Take the app live and hand off

After the working-site conversation is confirmed and every node is done:

**Nothing here waits on the workers.** Every node's branch was merged into the build
branch as it finished, so by now the workers hold nothing the app needs. Taking the app
live comes first and the teardown happens afterwards, while the user already has it.

1. **Check the build branch has everything** (Step 4, item 7). A `git log` against the
   branch, not a reason to stop any agent first.
2. **Merge into main** from the main checkout:
   `git merge --no-ff "build-app/$APP"`. Every node was merged into that branch
   as it finished, so this brings the whole build over in one commit. A conflict
   here means main changed during the build -- usually another app added to the
   root `pyproject.toml`. Keep both sides, and never hand-resolve by dropping
   either app's entry.
3. **Start it for real:**
   `uv sync --all-packages`, then `supervisorctl reread && supervisorctl update`,
   then `supervisorctl status "$APP"`. Verify it with
   `.agents/skills/build-app/references/verify.md`, and open the window with
   `python3 system/scripts/layout.py open "$APP"`. **The user has the app from here**,
   so everything below runs while they are looking at it.
4. **Hand off to hardening** exactly as `build-app` Step 5 does: invoke the
   `crystallize-creation` skill with `type=app`, the slug `$APP`, and a task body
   naming the lib path, the app name, the URL segment, and what the app does.
   That single hardening pass is the only thorough test-and-review run the app
   gets; no worker ran one. It runs in a worker of its own, so start it before the
   teardown rather than after it.
5. **Stop the workers.** Destroy every remaining `$APP-node-*` worker, which
   removes its worktree.
6. **Remove the folders.** List `$BUILD` first (`git -C "$BUILD" status
   --porcelain` must be empty, since every node was committed and merged), then
   `git worktree remove "$BUILD"` and `git worktree prune` to clear out the
   worktrees of any workers already destroyed. The `mngr/$APP-node-*` branches
   stay: they are the per-node history behind the merge.

## When things go wrong

- **The planner or plan fails twice:** Step 2.
- **A worker reports `stuck`, or its worker is gone while you are waiting on it:**
  Step 4, item 6.
- **A create failed and cleaned up its worktree:** Step 3. Nothing else was
  touched, so relaunch the node.
- **A merge conflicts:** two nodes that ran side by side wrote the same file,
  which the plan is meant to prevent. Nothing was lost -- both versions are on
  their own branches. If the two changes are independent, keep both sides and
  carry on. If they genuinely disagree, `git -C "$BUILD" merge --abort`, stop
  launching, tell the user which piece is in question, and have the worker that
  owns the file redo its part from the merged branch once the other is in.
- **A worker did more than its subtask** (its report lists files or work the
  subtask did not name, or `git -C "$BUILD" show --stat` for its merge names
  files no report accounts for): do not build on the extra work. Before the next
  launch, have that worker revert what falls outside its subtask on its own
  branch and report again, then merge that.
