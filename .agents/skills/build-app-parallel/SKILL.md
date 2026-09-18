---
name: build-app-parallel
description: "Use when you want to create a new app for the user -- a page, dashboard, or tool they can open as a tab. A planner splits the build into parts, workers build those parts side by side in one shared folder, and you handle every contact with the user (including the throwaway mock and working-site reviews) and take the confirmed app live. For changing or removing an existing app use update-app."
metadata:
  author: imbue
---

# Building an app in parallel

You orchestrate. You do not build the app yourself:

- **A planner** reads `build-app` and the workspace and writes a plan: a small
  graph of nodes, each one piece of the build, with what each depends on. It is
  the offline plan recorder's planner, run in the foreground with the prompt
  `system/scripts/imbue_plan_extra/prompts/build-app-parallel.md`.
- **Workers** build the nodes, several at once, all inside one git checkout made
  for this build (the build folder). Each follows
  `references/worker-node.md` and reports back.
- **You** clarify the request, run the planner, launch workers as their
  dependencies finish, run the interactive nodes (every contact with the user), merge the
  result, and hand the app to hardening.

`.agents/skills/build-app/SKILL.md` is the reference for how an app is built
here. The planner and the workers read it; do not follow its steps yourself. Its
Step 0 (clarify) and Step 5 (hand off to `crystallize-creation`) are yours, and
are restated below.

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
| Build folder | `$HOME/worktrees/build-app-parallel-$APP` (call it `$BUILD`) |
| Build branch | `build-app-parallel/$APP` |
| Worker for node N | `$APP-node-N` |
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

Run the planner in the foreground, with the longest tool timeout you can give
it; it takes a few minutes, and waiting it out inside your turn keeps the floor.
Send the user nothing while it runs:

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

## Step 3: Set up the build folder

The workers start from your last commit, so commit any pending changes in the
main checkout first (commit, never stash). Then:

```bash
git worktree add -b "build-app-parallel/$APP" "$BUILD" HEAD
(cd "$BUILD" && uv sync --all-packages)
```

Copy anything under `data/` the build needs (such as a handed-off `sample.json`)
into `$BUILD` at the same relative path; `data/` is not part of the checkout.

**Known mngr bug:** if `mngr create` fails partway (for example on a Claude Code
version mismatch), its cleanup runs `git worktree remove --force` on the folder
it was given, which deletes `$BUILD` with every worker's uncommitted work. Until
that is fixed, commit the build folder whenever no worker is running (Step 4),
and if `$BUILD` disappears: run `git worktree prune`, recreate it with
`git worktree add "$BUILD" "build-app-parallel/$APP"`, run the sync again, tell
the user the build lost its most recent work, and relaunch the nodes that were
running.

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
       --template shared_folder_worker \
       --work-folder "$BUILD" \
       --runtime-dir "$RUN/nodes/N/" \
       --task-file "$RUN/nodes/N/task.md" \
       --create-message "Your task is in \`$RUN/nodes/N/task.md\`, relative to this folder. Read it and do what it says." \
       --create-arg=--foreground \
       --create-arg=-S \
       --create-arg=agent_types.headless_claude.settings_overrides.model=<model> \
       --detach
   ```

   Add N to `running` in `$RUN/progress.txt`, and launch every ready node before
   you wait for any of them -- that is what makes them run at once.

   The last five options are what a shared-folder worker needs, and each one
   fails in a different way if it is dropped:

   - `--create-message` carries the task, because these workers are headless and
     cannot be messaged once running. Point at the task file rather than passing
     it: a create-time message reaches the agent as a command-line argument, and
     a task file's opening `---` is read there as an unknown option.
   - `--create-arg=--foreground` is required by mngr for a headless type, and
     `--detach` goes with it: that create runs the worker rather than starting
     it, so without `--detach` the launch would not return until the worker had
     finished, and the nodes would run one at a time.
   - The model override names `headless_claude`, the type these workers actually
     resolve to. Naming `claude` instead is rejected, because a setting is its
     own config layer and has to name the right type.
   - Write each `--create-arg` joined with `=`, or an argument starting with `-`
     is read as an option of `launch` itself.

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
     it. Move N from `running` to `done`. If a later interactive node reviews
     what this node built, keep the worker running so it can apply the user's
     changes. Otherwise destroy it:
     `uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name "$APP-node-N"`.
     Destroying a worker leaves `$BUILD` intact.
   - **Exit 76 (the worker's agent is not running and no report arrived).** A
     worker that has not yet started its first turn reads the same way as one
     that stopped early, and the check gives up after about fifteen seconds, so
     read this as "not working *yet*", not "broken". Look for a delivered report
     first (`$RUN/nodes/N/reports/`), and if there is none, nudge it once:

     ```bash
     uv run .agents/skills/launch-task/scripts/create_worker.py reply \
         --task-file "$RUN/nodes/N/task.md" \
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

6. **Commit when no worker is running:**

   ```bash
   git -C "$BUILD" add -A
   git -C "$BUILD" commit -m "build-app-parallel $APP: nodes <done indices>"
   ```

   Workers never commit, so this is the only history the build has.

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

1. **Serve a preview from the build folder.** The app is not live yet, so show
   a throwaway instance wrapped in a labeled preview tab, with its own scratch
   data folder:

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
          --task-file "$RUN/nodes/K/task.md" -m "<the change>"
      ```

      Then wait on it again (Step 4, item 4). The `await` that printed the
      builder's last report already archived it, so its next report lands
      cleanly.
   2. When its new report lands, run
      `python3 system/scripts/layout.py refresh "$APP-preview"` and show the user
      the change visibly applied.

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

1. **Stop the workers.** Destroy every remaining `$APP-node-*` worker, then
   commit the build folder (Step 4, item 6).
2. **Merge into main** from the main checkout:
   `git merge --no-ff "build-app-parallel/$APP"`. The plan keeps workers out of
   each other's files, so a conflict here means main changed during the build --
   usually another app added to the root `pyproject.toml`. Keep both sides, and
   never hand-resolve by dropping either app's entry.
3. **Start it for real:**
   `uv sync --all-packages`, then `supervisorctl reread && supervisorctl update`,
   then `supervisorctl status "$APP"`. Verify it with
   `.agents/skills/build-app/references/verify.md`, and open the tab with
   `python3 system/scripts/layout.py open "$APP"`.
4. **Remove the build folder.** List it first (`git -C "$BUILD" status --porcelain`
   must be empty, since everything was committed and merged), then
   `git worktree remove "$BUILD"`.
5. **Hand off to hardening** exactly as `build-app` Step 5 does: invoke the
   `crystallize-creation` skill with `type=app`, the slug `$APP`, and a task body
   naming the lib path, the app name, the URL segment, and what the app does.
   That single hardening pass is the only thorough test-and-review run the app
   gets; no worker ran one.

## When things go wrong

- **The planner or plan fails twice:** Step 2.
- **A worker reports `stuck`, or its worker is gone while you are waiting on it:**
  Step 4, item 5.
- **`$BUILD` disappears:** the mngr bug in Step 3.
- **Two workers edited the same file** (a report says so, or the preview shows
  one piece overwriting another): stop launching, tell the user, and have the
  worker that owns the file redo its part once the other is done.
- **A worker did more than its subtask** (its report lists files or work the
  subtask did not name, or `git -C "$BUILD" status` at a commit point shows
  changes no report accounts for): do not build on the extra work. Before the
  next launch, have that worker revert what falls outside its subtask, and check
  that no other node's files were changed.
