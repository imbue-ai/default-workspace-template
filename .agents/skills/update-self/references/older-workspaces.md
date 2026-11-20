# Older workspaces: what differs, and when each difference can go

A pass runs the target release's copy of this skill against a workspace whose
own tree, launcher and installed tools are whatever release it is still on.
Each section here is what to do about one difference such a workspace shows.
The step that found the difference sent you to its section, and nothing else
in the skill carries such text. What differs for the worker is in
`worker/older-workspaces.md`.

**The floor** is the oldest release a workspace updates from in one pass:
minds-v0.3.17, which `python/update_self_skill/launcher_contract_test.py`
pins. Every item below goes once minds-v0.10.0 is the stable release and the
floor is raised to it. Each item's `CLEANUP:` line names what to remove, and
the lowest floor at which that item could go by itself.

## Contents

- The history rewrite and shallow clones
- A system Python older than 3.12
- Sending the worker a message without `message_chat.py`
- Consuming a report by hand
- Apps built before `stop_when_no_windows`
- The first apply that lands the recovery machinery

## The history rewrite and shallow clones

Two one-time events in the template's history. Step 2, Step 3a and the
worker's Step 1 each carry a one-line call for them, a no-op on every other
workspace.

**Shallow clones.** A workspace made from a pool host baked before the
full-history bake fix carries a `--depth 1` clone: `git log` dead-ends at a
parentless graft commit, `git describe` fails, and "what changed since the
fork point" has no answer. The upstream template is public, so completing the
history is one fetch. The lead (before resolving the target) and the worker
(before its merge) run the same guarded lines:

```bash
if [ -f "$(git rev-parse --git-common-dir)/shallow" ]; then
    git fetch --unshallow upstream
fi
```

The guard is what makes it a no-op elsewhere: `--unshallow` errors on a repo
that is not shallow. `--git-common-dir`, not `--git-dir`, because the shallow
marker is repo-wide state in the common dir, which a worktree shares, so the
worker's call heals the whole workspace. The lines ship in the target
release's copy of the skill, so the heal applies on the first update into a
release that carries them.

**The rewrite.** The template's history was rewritten once, so a workspace
made before that shares no commit with `$REF`, and the worker's merge would
have no base. Step 3a's `bridge-history --ref "$REF"` grafts the workspace's
fork point onto its rewritten twin in `$REF` for the rest of the pass. The
workspace's own history is not touched, teardown drops the graft, and either
`"bridged"` value the command prints needs nothing from the user. Until
the bridge is built such a workspace has no merge base, so Step 2's preview
of the release waits until after Step 3a.

A clone whose shallow boundary predates the rewrite cannot be healed:
upstream no longer has those commits, so the fetch succeeds and the clone
stays shallow. `bridge-history` matches that boundary to its rewritten twin
instead, which is what gives such a workspace its merge base.

CLEANUP: remove the guarded fetch from SKILL.md Step 2 and the worker guide's
Step 1, `bridge-history` and `update_history_bridge.py`, its calls in SKILL.md
Step 3a, `teardown.md` and the two resuming references, and this section,
once no supported workspace predates the bake fix or the rewrite
(`update_history_bridge.py` carries the same marker).

## A system Python older than 3.12

A local Lima workspace made before 2026-09-14 runs Debian 12, whose system
`python3` is older than the 3.12 this skill's code needs. The staged
`scripts/update_self.py` exits 75 on every subcommand there and prints the
whole procedure: record `NEEDS_RECREATION` with the workspace's own copy,
tell the user as `apply-outcomes.md` ("Migration-required updates") says,
and release the lease. Follow what it printed. When the workspace's own copy
rejects `run-status` as an invalid choice, it is from before minds-v0.4.3
and recorded no pass, so skip the verdict.

CLEANUP: remove the guard in `scripts/update_self.py`, `version_guard_test.py`,
the exit 75 clause in SKILL.md Step 3a and this section once no supported
workspace runs a system Python older than 3.12.

## Sending the worker a message without `message_chat.py`

`applying-the-update.md` ("Messaging the worker") sends through the chat
app's `system/scripts/message_chat.py`, addressed by the `worker_agent_id`
the launcher stamps into the task file, and `question-gates.md` answers a
gate with `create_worker.py reply`. A workspace from before minds-v0.6.1 has
none of the three. Send with `mngr message`, which also starts a worker that
stopped, with `MESSAGE` set to the message or to the reply:

```bash
WORKER_ID=$(sed -n 's/^worker_agent_id: *//p' data/.tasks/update-self/task.md)
mngr message "${WORKER_ID:-update-self}" --start -m "$MESSAGE"
```

A non-zero exit, or a `No agents found` line, means the message did not reach
the worker: handle it as `applying-the-update.md` ("Messaging the worker")
handles a failed send.

CLEANUP: remove this section, its pointers in `applying-the-update.md` and
`question-gates.md`, and the `mngr message` fallback in
`python/update_self_skill/run_in_background.py` at a floor of minds-v0.6.1.

## Consuming a report by hand

A launcher from minds-v0.6.2 on moves a report into
`data/.tasks/update-self/reports/consumed/` as `await` returns it. An older
one leaves it at `data/.tasks/update-self/reports/report.md`, where a re-armed
poll would return it again at once and the next pass's `launch` refuses to
start over it. Move it aside at the point the step that sent you here names:

```bash
mkdir -p data/.tasks/update-self/reports/consumed
[ ! -e data/.tasks/update-self/reports/report.md ] || mv data/.tasks/update-self/reports/report.md \
    data/.tasks/update-self/reports/consumed/$(date +%s)-report.md
```

Five places point here:

- `question-gates.md`, "Answering and re-arming": before the reply to a
  gate, which `auditing-the-report.md` also uses to send a report back.
- `question-gates.md`, "A hold ends the pass with a decision": after the
  worker is stopped.
- `applying-the-update.md`, "Catching the worker up": before the worker is
  messaged.
- `apply-outcomes.md`, "Exit 2": before the retry is sent.
- `teardown.md`, "After a landed apply": before the worker is stopped.

CLEANUP: remove this section and those five pointers at a floor of
minds-v0.6.2.

## Apps built before `stop_when_no_windows`

From minds-v0.8.1 the shell stops an app whose manifest declares
`stop_when_no_windows = true` a minute after its last window closes, and
starts it again on the next request (the `build-app` skill says which apps
should carry it). A manifest without the field reads as `false`, so an app
built before the field existed keeps running for the life of the workspace,
as it always did.

After a landed apply, for each `system/apps/*/app.toml` that is the user's and
lacks the field, read the app. Do not add the field mechanically:

- It only answers requests: add `stop_when_no_windows = true`, and restart
  its program so the registration carries it.
- It does work between requests (a background thread, a poller or scheduled
  refresh, a subscription to an outside service, a job that outlives the
  window, an API another agent drives with no window): add
  `stop_when_no_windows = false` with a comment saying why, so the next pass
  does not ask again.

Name each decision in the results message.

CLEANUP: remove this section and its pointer in `after-the-apply.md` once
every user app in the fleet declares the field (a floor of minds-v0.8.1 is
not enough: an app built before it may still lack the field).

## The first apply that lands the recovery machinery

An interrupted apply heals itself through two triggers that live in the
running container: bootstrap's rollback of a stale marker, and the
`recover --if-stale` cron. On the one apply that first lands them into a
workspace (an update from before minds-v0.4.3), neither exists yet, and an
interruption before the restart is only recoverable by re-running the apply
by hand. Say so to the user before you run the apply.

CLEANUP: remove this section and its pointers in `applying-the-update.md` and
`apply-outcomes.md` at a floor of minds-v0.4.3.

