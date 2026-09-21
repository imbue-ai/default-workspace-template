---
name: build-app-parallel
description: "Use when you want to create a new app for the user -- a page, dashboard, or tool they can open as a tab. A planner splits the build into parts, workers build those parts side by side in a git worktree each, and you merge their work, handle every contact with the user (including the throwaway mock and working-site reviews) and take the confirmed app live. For changing or removing an existing app use update-app."
metadata:
  author: imbue
---

# Building an app in parallel

You orchestrate. You do not build the app yourself:

- **A planner** reads `build-app` and the workspace and writes a plan: a small
  graph of nodes, each one piece of the build, with what each depends on. It is
  the offline plan recorder's planner, run in the foreground with the prompt
  `system/scripts/imbue_plan_extra/prompts/build-app-parallel.md`.
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

`.agents/shared/build-app/README.md` is the reference for how an app is built
here. The planner and the workers read it; you open it to look something up, and
never to follow. In particular its first step fires a plan recorder that is not
yours -- Step 2 below runs the planner this build uses. An eval run opened that
file, followed it from the top, and built the whole app by hand: no plan, no
workers, no reviews, and nothing to show for the flow it was supposed to be
running. Its Step 0 (clarify) and Step 5 (hand off to `crystallize-creation`)
are yours, and are restated below.

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
| Run folder (plan, tasks, reports) | `data/.tasks/build-app-parallel/$APP/` (call it `$RUN`) |
| Integration folder | `$HOME/worktrees/build-app-parallel-$APP` (call it `$BUILD`) |
| Build branch | `build-app-parallel/$APP`, checked out in `$BUILD` |
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

Show the user stages, not nodes. Create these steps up front, in order, and
close each when its stage ends:

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

## Step 1: Clarify (business terms only)

Ask only the questions that genuinely block: a fork that is both genuinely
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
mkdir -p "$RUN"
cat > "$RUN/brief.md" <<'BRIEF'
<the brief>
BRIEF
```

The integration folder does not depend on the plan, and its `uv sync` takes
about as long as the planner does, so start it first and let the two run
together -- otherwise nothing at all happens for the first four minutes of a
build. It is where every node's branch is merged and where the previews are
served from, so it needs a working checkout of its own. Commit any pending
changes in the main checkout (commit, never stash; the build branch starts from
your last commit), then:

```bash
git worktree add -b "build-app-parallel/$APP" "$BUILD" HEAD
(cd "$BUILD" && nohup uv sync --all-packages > "$RUN/sync.log" 2>&1 &)
```

Then run the planner in the foreground, with the longest tool timeout you can
give it; waiting it out inside your turn keeps the floor. Send the user nothing
while it runs:

```bash
system/scripts/imbue_plan_extra/write_plan.sh --run-dir "$RUN" build-app-parallel
```

When it exits 0, check the plan:

```bash
python3 .agents/skills/build-app-parallel/scripts/plan_orchestration.py parse --run-dir "$RUN"
```

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
`uv sync` runs as part of its create, so a worker can start before this
finishes.

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
   python3 .agents/skills/build-app-parallel/scripts/plan_orchestration.py ready \
       --run-dir "$RUN" --done <done indices> --running <running indices>
   ```

   It prints comma-separated node indices. It never starts more than 5 workers at
   once, and interactive nodes take no worker slot.

2. **Launch each worker node it printed, one node per command.** Never put two
   launches in one shell command: they run one after the other anyway, and a
   batched launch hides every worker after the first from the evidence an eval
   collects. Look up the node's `model` in `$RUN/plan.json`, then:

   ```bash
   python3 .agents/skills/build-app-parallel/scripts/plan_orchestration.py write-task \
       --run-dir "$RUN" --node N
   uv run .agents/skills/launch-task/scripts/create_worker.py launch \
       --name "$APP-node-N" \
       --template worktree_worker \
       --runtime-dir "$RUN/nodes/N/" \
       --task-file "$RUN/nodes/N/task.md" \
       --create-arg=--branch \
       --create-arg="build-app-parallel/$APP:mngr/$APP-node-N" \
       --create-arg=-S \
       --create-arg=agent_types.claude.settings_overrides.model=<model> \
       --message-with-mngr
   ```

   Add N to `running` in `$RUN/progress.txt`, and launch every ready node before
   you wait for any of them -- that is what makes them run at once.

   What these options are for:

   - `--branch <build branch>:mngr/$APP-node-N` is the one that matters. It
     branches the worker's worktree off the build branch **as it stands right
     now**, which is how the node sees everything merged before it, and it names
     the branch you will merge back. Without it mngr branches from your own
     checkout's HEAD, and the node would start from a workspace where none of
     the build has happened.
   - `--message-with-mngr` sends the task with `mngr message`, and you pass it to
     `reply` too. These workers are not chats anyone opens, and the chat app's
     send route knows an agent only once it has re-read mngr's agent list, so a
     worker messaged seconds after its create can fall into the window where it
     answers 404 -- and its success answer means "delivered or queued" either
     way. That window is where a task went missing and left two workers idle.
   - The `-S` pair sets the model for this one worker. Write each `--create-arg`
     joined with `=`, or an argument starting with `-` is read as an option of
     `launch` itself.

3. **Start each interactive node it printed** with Step 5. Add it to `running`.

4. **Wait for the running workers, in the foreground, one at a time.** Take them
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

5. **Handle each report as its `await` returns.** Follow
   `.agents/shared/references/lead-proxy.md` for reading the report, diagnosing
   a timeout, and a worker stopped for memory (exit 75) -- everything but its
   polling advice, which item 4 replaces. The reports dir is
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
     incompatible, stop launching and follow the same entry.

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

6. **Nothing to commit.** Each worker commits its own piece, and item 5 merges
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
   not merged is not in it. The app is not live yet, so show a throwaway
   instance wrapped in a labeled preview tab, with its own scratch data folder:

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

      Then wait on it again (Step 4, item 4). The `await` that printed the
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

1. **Stop the workers.** Destroy every remaining `$APP-node-*` worker, which
   removes its worktree, and check the build branch has everything (Step 4,
   item 6).
2. **Merge into main** from the main checkout:
   `git merge --no-ff "build-app-parallel/$APP"`. Every node was merged into that
   branch as it finished, so this brings the whole build over in one commit. A
   conflict here means main changed during the build -- usually another app added
   to the root `pyproject.toml`. Keep both sides, and never hand-resolve by
   dropping either app's entry.
3. **Start it for real:**
   `uv sync --all-packages`, then `supervisorctl reread && supervisorctl update`,
   then `supervisorctl status "$APP"`. Verify it with
   `.agents/shared/build-app/references/verify.md`, and open the tab with
   `python3 system/scripts/layout.py open "$APP"`.
4. **Remove the folders.** List `$BUILD` first (`git -C "$BUILD" status
   --porcelain` must be empty, since every node was committed and merged), then
   `git worktree remove "$BUILD"` and `git worktree prune` to clear out the
   worktrees of any workers already destroyed. The `mngr/$APP-node-*` branches
   stay: they are the per-node history behind the merge.
5. **Hand off to hardening** exactly as `build-app` Step 5 does: invoke the
   `crystallize-creation` skill with `type=app`, the slug `$APP`, and a task body
   naming the lib path, the app name, the URL segment, and what the app does.
   That single hardening pass is the only thorough test-and-review run the app
   gets; no worker ran one.

## When things go wrong

- **The planner or plan fails twice:** Step 2.
- **A worker reports `stuck`, or its worker is gone while you are waiting on it:**
  Step 4, item 5.
- **A create fails:** Step 3's note -- the worker's own worktree is what gets
  cleaned up, so relaunch the node.
- **A merge conflicts** (Step 4, item 5): two nodes that ran side by side wrote
  the same file, which the plan is meant to prevent. Nothing was lost -- both
  versions are on their own branches. If the two changes are independent, keep
  both sides and carry on. If they genuinely disagree, `git -C "$BUILD" merge
  --abort`, stop launching, tell the user which piece is in question, and have
  the worker that owns the file redo its part from the merged branch once the
  other is in.
- **A worker did more than its subtask** (its report lists files or work the
  subtask did not name, or `git -C "$BUILD" show --stat` for its merge names
  files no report accounts for): do not build on the extra work. Before the next
  launch, have that worker revert what falls outside its subtask on its own
  branch and report again, then merge that.
