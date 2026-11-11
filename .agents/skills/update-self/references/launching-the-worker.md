# Launching the worker (SKILL.md Step 3b)

Follow this top to bottom once Step 3a has cleared the ceiling, built the
bridge and staged the shared prose. It asks of the launcher only what every
supported launcher has, and writes the task file's report address itself;
`older-workspaces.md` ("The launcher is the workspace's own") says why, so do
not swap a command here for a newer launcher subcommand.

Do not open a window for your own chat while the pass runs: teardown opens it
once the results are ready.

## Contents

- 1. Open the tracking ticket
- 2. Write the task file
- 3. Clear the previous pass's worker
- 4. Clear the previous pass's branch
- 5. Launch, record the hand-off, arm the poll

## 1. Open the tracking ticket

Note the id it prints, then `tk start <ticket-id>` as its own tool call:

```bash
mkdir -p data/.tasks/update-self
tk create "update-self" -t task \
    --acceptance "worker launched; conflicts triaged; validated; branch applied"
```

## 2. Write the task file

An **unquoted** frontmatter heredoc, so `$MNGR_AGENT_ID` and `$REF` expand,
then a **quoted** body. The `lead_agent` line must stay. It is the lead's
agent id, not its name: a rename of the lead's chat mid-pass would otherwise
strand the worker's report.

```bash
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_ID
finish_report_path: data/.tasks/update-self/reports/report.md
target_ref: $REF
---
FRONTMATTER_EOF
cat << 'BODY_EOF'

# Task: safe update-self

## What to do
Follow the worker guide at
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self/references/update-self-worker.md`
end to end: trial-merge conflict triage, complete the merge (preserving the
`update-self:` merge-commit subject), validate the merged set, generate the
"what's new" report, and report `done`. That
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self` path is the copy
of the update-self skill shipped with the release being updated to (staged by the
lead and synced into your worktree with this runtime dir) -- run *all* its
`update_self.py` calls from its `scripts/` too. Your target is the `target_ref` in
this file's frontmatter (already fetched into `upstream`).

## Reporting back
Per Step 6 of the worker guide: the report shapes come from
`data/.tasks/update-self/skill-at-target/.agents/shared/references/worker-reporting.md`, but you write the report
and copy it into place by hand as Step 6 spells out -- not with the launcher's
`report` subcommand. Valid `name:` values:
`question` (mid-flight gate: a genuine, unresolvable conflict, the Step 4a/4b/4c
scope escape hatch, a gate command shed for memory, or a Step 4b customization the
update cannot keep), `done` / `stuck` (terminal). `<TASK_FILE>` -> `data/.tasks/update-self/task.md`.
BODY_EOF
} > data/.tasks/update-self/task.md
```

## 3. Clear the previous pass's worker

Teardown leaves the last pass's worker *stopped*, so its transcript stays
reachable for bug reports. Look for it:

```bash
mngr list --format "{name}	{state}" 2>/dev/null | grep -P "^update-self\t"
```

A worker of that name in state `STOPPED` or `DONE` is destroyed:

```bash
mngr destroy update-self --force
```

One in any other state is still working. That is a genuine conflict with
another pass: resolve it as SKILL.md Step 1's lease check does, and never
force past it.

## 4. Clear the previous pass's branch

Do this whether or not a worker was listed: destroying a worker leaves its
`mngr/update-self` branch behind, and the launch cannot create the worker
while a branch of that name exists. First ask whether `HEAD` already has it:

```bash
git merge-base --is-ancestor refs/heads/mngr/update-self HEAD
```

- **A `Not a valid object name` error**: there is no such branch, and nothing
  to clear.
- **Exit 0**: `HEAD` has it, as after a landed pass (a rollback is a revert
  commit on top of the merge). Delete it. `-D`, because the check above is the
  safety check: `-d` asks the branch's pushed copy instead of `HEAD` once
  GitHub sync has pushed it.

  ```bash
  git branch -D mngr/update-self
  ```

- **Exit 1**: the branch holds commits `HEAD` does not have. Keep them under
  an archive name, and note the name it prints:

  ```bash
  ARCHIVE="archive/update-self-$(date +%Y%m%d-%H%M%S)"
  git branch -m mngr/update-self "$ARCHIVE" && echo "$ARCHIVE"
  ```

  The results message then carries a plain caveat that unfinished work from
  an earlier update attempt was set aside and kept, and can be recovered on
  request. The archive name itself goes in the tracking ticket's close
  summary, not the message.

## 5. Launch, record the hand-off, arm the poll

Launch with the plain `worker` template:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-self --template worker \
    --runtime-dir data/.tasks/update-self/ --task-file data/.tasks/update-self/task.md
```

Record the hand-off. From here until the worker reports, this chat has ended
its turn but is busy on the poll, so the Imbue Studio app reads it as
updating rather than "waiting for you"; naming the worker lets the app read
the worker's liveness as well:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status delegate update-self
```

Then poll in the background, through the staged skill's own runner, which
delivers `await`'s result to this chat as a message that starts your next
turn, whatever your harness:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/run_in_background.py \
    --description "Wait for the update's background agent" --waiter -- \
    uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py await \
    --name update-self --task-file data/.tasks/update-self/task.md --timeout 90m
```

Once the poll is armed, **end your turn**; its message wakes you with the
report. Re-arm with the same command after every reply to the worker. Never
wait on the worker any other way: no `sleep`, no polling its reports
directory or its pane. "Never sleep on a worker" in
`data/.tasks/update-self/skill-at-target/.agents/shared/references/lead-proxy.md`
has the reasons.
