# Older workspaces: what differs, and when each difference can go

A pass runs the target release's copy of this skill against a workspace whose
own tree, launcher and installed tools are whatever release it is still on
(`handoff-contract.md`). Everything here is true only for a workspace older
than some release. Nothing else in the skill carries such text: a step that
has a difference points at its section here in one line.

Read it whole when SKILL.md Step 3 sends you here, because another copy of
this skill ran your Steps 1-2. Otherwise read the one section a step names.

**The floor** is the oldest release a workspace updates from in one pass:
minds-v0.3.17, which `python/update_self_skill/launcher_contract_test.py`
pins. Every item below goes once minds-v0.10.0 is the stable release and the
floor is raised to it. Each `CLEANUP:` line names what to remove, and the
lowest floor at which that item could go by itself.

## Contents

For the lead:

- A copy of Steps 1-2 that came from an older release
- The history rewrite and shallow clones
- A system Python older than 3.12
- The launcher is the workspace's own
- Sending the worker a message without `message_chat.py`
- Consuming a report by hand
- Apps built before `stop_when_no_windows`
- A stray preview of a critical app
- A tree without `workspace-layout show`
- The first apply that lands the recovery machinery

For the worker:

- Delivering the report by hand
- Cron entries from before routines
- Apps from before the app model
- Python from before the packaged layout

## A copy of Steps 1-2 that came from an older release

Steps 1-2 ran from the workspace's own copy of this skill, so they did what
that release's Steps 1-2 did, not what the current SKILL.md says.

- **The ceiling.** A copy from before the ceiling check resolves the newest
  tag upstream, which is the too-new target the ceiling exists to refuse, and
  has already told the user that version. Step 3a's re-check is the only one
  that runs for such a workspace: the staged copy is by construction at least
  as new as `$REF`, so it checks however old the initiator was. When it moves
  `$REF` to the capped ref, say which version you are updating to; the user
  heard the other one.
- **The pin.** The app's starting message carries the target and the ceiling.
  A Step 2 from before minds-v0.9.0 does not read them, and follows the
  message's own instruction to append `--ceiling <ref>` to `resolve-target`.
  Step 3a still takes the `Ceiling:` of that message as `PINNED_CEILING`.
- **Tags.** An older copy of Step 2 cannot move a tag the workspace already
  holds. Step 3a therefore fetches the tags again, forced, before it builds
  the bridge, so the bridge is built against the commit the worker will merge.
- **`run-status`.** A copy from before minds-v0.4.3 has no `run-status`, so
  its Step 1 recorded no pass. From Step 3 on every `run-status` call runs
  the staged copy, which also keeps the record of the apps a pass stopped; an
  older own copy would drop that record.

CLEANUP: remove the ceiling item at a floor of minds-v0.3.10, the pin item and
`--ceiling` as a spelling of `--app-version` (`update_self.py`,
`handoff-contract.md`) at minds-v0.9.0, and the `run-status` item at
minds-v0.4.3. Step 3a's re-check and its forced fetch stay: they are the
hand-off contract.

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
worker's call heals the whole workspace.

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

## The launcher is the workspace's own

The lead follows the staged prose but launches with the workspace's own
`launch-task` launcher. Staging a newer launcher would not help: it may depend
on settings or plugins the merge has not landed yet. So `launching-the-worker.md`
asks of `create_worker.py` only the subcommands and flags the floor's launcher
has (`launcher_contract_test.py` lists them), which is why:

- **The task file writes `lead_agent` and `finish_report_path` itself.** A
  launcher from before launch-time stamping leaves the worker with no report
  address: it finishes, cannot deliver, and the lead waits out the timeout.
- **The previous pass's worker and branch are cleared with plain `mngr` and
  `git` commands**, not launcher subcommands the floor lacks.
- **The poll runs the staged skill's own `scripts/run_in_background.py`.** A
  tree from before minds-v0.8.0 has no such script of its own. The staged one
  is standard-library only, and sends through `system/scripts/message_chat.py`
  where the workspace has it, else `mngr message`.
- **A reply to a gate** goes through `create_worker.py reply` only where the
  launcher has it (minds-v0.6.1 on). Before that, send the reply as the next
  section sends a message.

CLEANUP: at a floor of minds-v0.10.0, `launching-the-worker.md` may use the
launcher's own subcommands (`stop`, `destroy`) and the task file may drop the
two frontmatter lines the launcher stamps; update `launcher_contract_test.py`
and `test_skill_md_task_template_carries_the_lead_agent_and_report_fields`
with them. The staged runner stays while the lead launches cross-version.

## Sending the worker a message without `message_chat.py`

`applying-the-update.md` ("Messaging the worker") sends through the chat
app's `system/scripts/message_chat.py`, addressed by the `worker_agent_id`
the launcher stamps into the task file. A workspace from before minds-v0.6.1
has neither. When the script is missing or the task file has no
`worker_agent_id` line, send with `mngr message`, which also starts a worker
that stopped:

```bash
WORKER_ID=$(sed -n 's/^worker_agent_id: *//p' data/.tasks/update-self/task.md)
mngr message "${WORKER_ID:-update-self}" --start -m "$MESSAGE"
```

A non-zero exit, or a `No agents found` line, means the message did not reach
the worker: handle it as that section handles a failed send.

CLEANUP: remove this section, its pointer in `applying-the-update.md`, and the
`mngr message` fallback in `python/update_self_skill/run_in_background.py` at
a floor of minds-v0.6.1.

## Consuming a report by hand

A launcher from minds-v0.6.2 on moves a report into
`data/.tasks/update-self/reports/consumed/` as `await` returns it. An older
one leaves it at `data/.tasks/update-self/reports/report.md`, where a re-armed
poll would return it again at once and the next pass's `launch` refuses to
start over it. When the report file is still there after the poll delivered
it, move it aside before you re-arm the poll, stop the worker, or end the
pass:

```bash
mkdir -p data/.tasks/update-self/reports/consumed
[ ! -e data/.tasks/update-self/reports/report.md ] || mv data/.tasks/update-self/reports/report.md \
    data/.tasks/update-self/reports/consumed/$(date +%s)-report.md
```

Three places need it: a hold, after the worker is stopped
(`question-gates.md`); a catch-up or retry, before the worker is messaged
(`applying-the-update.md`); and teardown after a landed apply
(`teardown.md`).

CLEANUP: remove this section and its three pointers at a floor of
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

## A stray preview of a critical app

A pass of a release before minds-v0.4.3 stood up previews of the critical
apps for the user to review, and may have left one registered. The careful
flow for a critical app refuses its next pass on that app while one is. When
`uv run --no-sync workspace-layout list` shows a `<name>-preview` of a
critical app, tear it down at teardown and close its window:

```bash
uv run --no-sync workspace-layout close <name>-preview
uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py down --app <name>
```

The close goes first: nothing takes a window away when its app leaves the
registry, so once `down` has deregistered the row the preview's window would
stay on the desktop pointing at a page nothing serves.

CLEANUP: remove this section and its pointer in `teardown.md` at a floor of
minds-v0.4.3, the first release whose pass stands up no previews.

## A tree without `workspace-layout show`

Teardown ends by showing the user this chat with `workspace-layout show`,
which a tree from before minds-v0.8.6 lacks. When the command fails there,
carry on: the app's update row still leads the user to this chat.

CLEANUP: remove this section at a floor of minds-v0.8.6. The "if it fails,
carry on" line in `teardown.md` stays for a shell that is not up.

## The first apply that lands the recovery machinery

An interrupted apply heals itself through two triggers that live in the
running container: bootstrap's rollback of a stale marker, and the
`recover --if-stale` cron. On the one apply that first lands them into a
workspace (an update from before minds-v0.4.3), neither exists yet, and an
interruption before the restart is only recoverable by re-running the apply
by hand. Say so to the user if that is the update you are running.

CLEANUP: remove this section and its pointer in `apply-outcomes.md` at a
floor of minds-v0.4.3.

## Delivering the report by hand

For the worker. The shared reporting reference delivers a report with the
launcher's `report` subcommand, which a launcher from before minds-v0.6.2
does not have, and the launcher in the worker's checkout is the workspace's
own. So the worker guide's Step 6 writes the report file and copies it into
place itself, which is all `report` does. A launcher from before minds-v0.6.1
also stamps no `lead_work_dir`, so `LEAD_WORK_DIR` may be unset: the lead's
checkout is then the main worktree of this same repo.

CLEANUP: at a floor of minds-v0.6.2, the worker guide's Step 6 and the task
file in `launching-the-worker.md` may deliver through `report` as the shared
reference says, and the `LEAD_WORK_DIR` fallbacks in Steps 4a and 6 can go.

## Cron entries from before routines

For the worker. From minds-v0.9.0 a scheduled task is a routine, a tracked
`system/routines/<id>.toml`. `routines migrate-cron` moved the cron drop-ins
of older workspaces across, but may have left one in place. Those live under
the gitignored `data/.state/cron.d`, so git shows none of them and your
worktree holds none (the launcher syncs only this pass's
`data/.tasks/update-self/` into it). Read the lead's copy: list
`$LEAD_WORK_DIR/data/.state/cron.d` (the main worktree of this same repo when
`LEAD_WORK_DIR` is unset, since a directory you cannot read answers exactly
like a workspace with no such entry) and grep its lines for every retired
name and every deleted or renamed path, as the worker guide's Step 4a greps
the routines. A hit is a user-created consumer, and the impact analysis runs
in full.

CLEANUP: remove this section and its pointer in the worker guide's Step 4a
once no supported workspace holds a cron drop-in (a floor of minds-v0.9.0,
and a fleet check for leftovers).

## Apps from before the app model

For the worker. From minds-v0.5.1 an app has an `app.toml` and the
`app-manifest` library computes its footprint. Two leftovers of the time
before:

- The worker runs `app-manifest` as `uv run --frozen --package app-manifest`,
  which installs the library from the merged tree. A workspace from before
  the app model has none of it, and the root project does not depend on it.
- An app directory with no `app.toml` has no footprint the library can
  compute. Its directory is its footprint: read `git diff` over it by hand for
  the two ranges `footprint-ranges` names.

CLEANUP: remove the second item, and its pointer in
`worker-impact-analysis.md`, once no supported workspace holds an app without
a manifest (a floor of minds-v0.5.1, and a fleet check). The `--package` form
stays while the worker reads the merged tree's library.

## Python from before the packaged layout

For the worker, and for the lead's repair worker. minds-v0.10.0 moved every
skill's Python into a package, made each user app, service and lib a
standalone uv project, and changed how commands are launched. A workspace
updating across that release has files of the user's in the old layout, and
`python-packaging-migration.md` is the guide to converting them: read it when
a failure in the worker's environment gate or suites, or a user program that
does not come back after the apply, is one of the symptoms in its table.

CLEANUP: `python-packaging-migration.md`, the `user-package-migration`
command and the items that name them in `worker-validation.md` and
`after-the-apply.md` go when `user_package_migration.py`'s own marker says
that module can.
