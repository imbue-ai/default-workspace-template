# Applying the update (SKILL.md Step 5b)

Follow this top to bottom once the audit has passed. Other chats keep working
while the worker runs, and nothing waits on them: they are paused only right
before the apply, for as long as it takes.

## Contents

- What does not block the apply
- 1. Check that the branch still fast-forwards
- Catching the worker up
- Messaging the worker
- 2. Pause the other chats
- 3. Save what the chats left uncommitted
- 4. Take the critical-app leases
- 5. Run the apply
- 6. Bring the chats back
- Landing a frontend change any other way

## What does not block the apply

Rebuild-only findings do not block the apply, and neither does a user-created
dependent the worker could not verify. They become leading caveats of the
results message:

- A global-dependency bump coupled to a user-created dependent: name it,
  check it after the apply, and offer a rollback or a workspace recreate.
- A container build or launch parameter a running container cannot adopt:
  say it stays inert until a recreate.

A genuinely breaking case is a migration-required update instead
(`apply-outcomes.md`, "Migration-required updates").

## 1. Check that the branch still fast-forwards

While the other chats still run, check whether they committed since the
worker branched:

```bash
git merge-base --is-ancestor HEAD mngr/update-self
```

Exit 0 means the branch still fast-forwards: go to step 2. Exit 1 means
`HEAD` moved under the pass, and the apply would refuse it: catch the worker
up, then come back here.

## Catching the worker up

A moved `HEAD` never calls for a fresh pass, so never destroy or stop the
worker to make room for one. The worker is still waiting, and holds the
branch and any bundles it built. A fresh pass (`launching-the-worker.md`) is
only for a worker that is gone, or one that reports `stuck` on the catch-up
and is not resumed (`resuming-after-stuck.md`).

If the `done` report you are answering is still in place, consume it first
(`older-workspaces.md`, "Consuming a report by hand"). Then message the
worker:

```bash
MESSAGE="Catch up to $(git rev-parse HEAD) per Step 7 of your worker guide, then report done again."
WORKER_ID=$(sed -n 's/^worker_agent_id: *//p' data/.tasks/update-self/task.md)
python3 system/scripts/message_chat.py "$WORKER_ID" -m "$MESSAGE"
```

Re-arm the poll (`launching-the-worker.md`, section 5) and end your turn.
Audit the `done` it sends back (Step 5a) before you return to step 1.

## Messaging the worker

A catch-up, a retry after a rollback (`apply-outcomes.md`, "Exit 2") and a
user's decision (`resuming-after-a-decision.md`) all reach the worker the
same way: `message_chat.py`, addressed by the agent id the launcher stamped
into the task file, with `MESSAGE` set in the same command. It starts a
worker that stopped. When `system/scripts/message_chat.py` does not exist, or
`WORKER_ID` comes back empty, send as `older-workspaces.md` ("Sending the
worker a message without `message_chat.py`") says.

Read its exit code:

- **0**: sent.
- **7**: it arrived, but the worker sits on a dialog it could not dismiss. Do
  not send it again. Re-arm the poll, and diagnose liveness as below if no
  report comes.
- **8**: the worker is gone. Take a fresh pass (`launching-the-worker.md`,
  with the same `$REF`, the lease and the run record carried over).
- **Any other non-zero exit**: the message did not reach the worker, so do
  not re-arm the poll on it. Check whether the worker is alive ("Diagnose
  worker liveness" in
  `data/.tasks/update-self/skill-at-target/.agents/shared/references/lead-proxy.md`)
  and send again. A worker that is gone takes a fresh pass.

## 2. Pause the other chats

Once the branch fast-forwards, pause every chat and background agent that is
busy (mid-turn, or waiting on a background command), other than this chat,
the update's own worker and anything under it:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status stop-chats
```

It records each one, so the resume in step 6 starts it again with a note, and
prints a line per agent it paused: keep those lines for the results message.
A busy main chat is stopped too, but the chat app starts it again at once,
idle, so it is interrupted rather than held, and the resume only sends it the
note.

A non-zero exit names what it could not pause and why. A listing that failed
paused nothing, so run it once more. An agent that would not stop keeps
running, which only means its edits may land beside the apply, as they always
could. Neither holds the apply back.

## 3. Save what the chats left uncommitted

One commit on the workspace's branch listing the paths, as SKILL.md Step 1
saves a dirty tree. Note the sha it prints for step 6:

```bash
git add -A
{ echo "Save work in progress before updating to $REF"; echo
  echo "Uncommitted changes, saved as they stood so the update could apply:"
  git diff --cached --name-only | sed 's/^/- /'; } > /tmp/update-self-wip-message.txt
git diff --cached --quiet || { git commit -F /tmp/update-self-wip-message.txt && git rev-parse HEAD; }
```

An `editing service <name>` lease another chat holds is no reason to wait:
that chat is paused now, and its edit is in this commit. An `editing critical
app` lease is no reason either: that flow edits in its own worktree, never
the served tree, and step 4 covers the critical apps this update touches.

A commit here, or one a chat made just before it paused, moves `HEAD` again.
Run step 1's check once more, and on exit 1 catch the worker up as above. The
chats stay paused through that round, which covers only those commits.

## 4. Take the critical-app leases

Only when the update touches any of these:

- a critical app's tree: `system/apps/system_interface/`, `system/apps/chat/`,
  `system/apps/terminal/`, `system/apps/terminal_pty/`;
- the frontend of the Getting Started, Files, Routines, Screen or Style Guide
  app: `system/apps/getting_started/frontend/`, `system/apps/files/frontend/`,
  `system/apps/routines/frontend/`, `system/apps/screen/frontend/`,
  `system/apps/style_guide/frontend/`;
- `system/libs/workspace_ui/`, `system/libs/workspace_layout/` or
  `system/libs/workspace_themes/`: the shared library compiled into every
  frontend, and the layout wire contract and theme library the critical apps
  run;
- `system/package.json` or `system/package-lock.json`, whose change rebuilds
  every frontend bundle.

Then take the `editing critical app <name>` lease for each critical app the
update touches, as `update-app/references/critical-app.md` does, and hold
them through the apply. `<name>` is the app's `app.toml` name, so
`system/apps/terminal_pty/` is `terminal-pty`. The five frontends above,
`workspace_ui` and the npm files count as both `system_interface` and `chat`.
`workspace_layout` and `workspace_themes` each count as `system_interface`,
`chat` and `terminal`.

Take them all or none:

1. Check each one in `tk ready` (`grep -E -- "- editing critical app
   <name>$"`, anchored so `terminal` does not match `terminal-pty`'s lease).
2. If any is held by another agent, release the ones you took and surface it
   instead of proceeding.
3. Take them in name order: `tk create "editing critical app <name>" -t
   chore`, then `tk start` it, each as its own command.

Take them only now, once the chats are paused and the last fast-forward check
has passed, so no catch-up round runs while you hold them. Release them
after the apply: in teardown when the pass ends there, and at once when the
apply's outcome sends the pass back to the worker (a retry after exit 2, a
refusal at exit 1). This step takes them again right before the next apply.

## 5. Run the apply

From the staged copy, in the **foreground**: its output (refusal and resume
messages, any provisioner warning, the `apply phase timings:` line) is what
you read before recording a verdict.

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref mngr/update-self --ff-only --target-ref "$REF"
```

When the report names the worker's **built frontend bundles**, append one
`--worker-bundle <app>=<path>` for each of the seven (`system_interface`,
`chat`, `getting_started`, `files`, `routines`, `screen`, `style_guide`), so
the exact builds the worker validated are installed instead of a live build.
The apply installs them only as a set (one `npm run build` emits them all),
and builds live when any is missing or stale.

That one command is the whole landing: it fast-forwards to the worker's
branch, refreshes, rebuilds and restarts what the update changed, probes the
workspace's health, and reverts the entire merge on any failure. It keeps its
own run record and raises no "recently updated" notice
(`--keep-rollback-point` is the careful flow's, not this one's).

`apply-outcomes.md` is the one home of its exit codes. In short: `0` applied,
`2` rolled back automatically, `3` emergency, `1` a precondition failed and
nothing changed. Read that file's section for the code you got before you do
anything else, and for exit 0 read the apply's closing stderr lines against
its "Exit 0" section before signing off.

## 6. Bring the chats back

Right after an apply that exits 0, 2 or 3, bring back what you paused, naming
the release when it landed (exit 0) and the commit that saved the uncommitted
work, when there was one:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status resume-chats --landed "$REF" --wip-commit <sha>
```

Leave `--landed` out after exit 2 or 3, and `--wip-commit` when nothing was
saved. After exit 1 the chats stay paused or come back by what refused the
apply (`apply-outcomes.md`, "Exit 1").

Each chat is started again and told what changed while it was paused, in a
notice the user sees labelled "Workspace updated". A background agent is
named to its lead chat, since it does not pick its work back up by itself.
The command is safe to re-run: a non-zero exit names what it could not start
or tell, and the same command finishes the rest.

## Landing a frontend change any other way

The served pages are the built `static/` bundles, not the source the update
changed, and the shared `workspace_ui` library is compiled into every one of
them, including behavior the shell depends on from every app page (the
`shell:input` activity reports its presence reads). An update applied without
the rebuild leaves every page running the old code while the tree says
otherwise. The apply command does the rebuild itself: a change under any
frontend source directory or `workspace_ui` re-emits the bundles. So the rule
matters only when landing such an update any other way: never hand-apply one
without running `npm run build` for every affected frontend.
