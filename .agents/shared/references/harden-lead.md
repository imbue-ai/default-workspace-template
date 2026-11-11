# Leading a harden pass

The dispatch steps the three lifecycle skills share: `crystallize-creation`,
`update-creation` and `heal-creation`. Your skill is the checklist: it names
its operation, carries each command with its own names filled in, and says what
goes in the task body and which gates and terminal statuses to expect. This
file carries what is the same for all three, in the order a pass meets it.

It names two other shared references by role, without sending you to them: the
harden-contention reference (one pass per creation, the checks before a merge,
catching a stale pass up) and the lead-proxy reference (polling, answering
gates, acting on a terminal status). Your skill names their paths at the steps
that need them.

## Contents

- Names and paths
- Single-flight check: before the ticket, for an update or heal pass
- The tracking ticket
- The task file
- Launching the worker
- Polling and proxying
- Before the merge
- Merge, then go live by type
- Closing the ticket

## Names and paths

- `$CREATIONS` is every creation the pass covers, each as `<type>:<name>`,
  space-separated: `app:<registered name>`, `skill:<directory>`,
  `service:<package>`, or `shared:<path under .agents/shared>` for a shared
  script or reference (e.g. `skill:migrate-config`, or `app:roadmap
  skill:sync-roadmap` for an app and the skill that drives it).
- Each creation's **type** is `skill`, `app`, `service`, or `shared` (handled
  as a skill). The worker reads it from the task file's `creations` and loads
  the type reference that matches; you use it for that creation's go-live.
- `$SLUG` names the pass: the creation's own name when there is one, otherwise
  a short kebab-case name for the change or the failure (`roadmap-sync`).
  `crystallize-creation` calls it `$NAME`, since it is the new creation's name.

Everything else follows from the operation (`crystallize`, `update` or `heal`)
and the slug:

| What | Value |
|---|---|
| Worker name | `<operation>-$SLUG` |
| Worker branch | `mngr/<operation>-$SLUG` |
| Runtime dir | `data/.tasks/harden/<operation>-$SLUG/` |
| Task file | `task.md` in the runtime dir |
| Scope file | `scope.json` in the runtime dir; the worker writes it |
| Report the poll waits for | `reports/report.md` in the runtime dir |
| Reports dir | `reports/` in the runtime dir. `await` archives each report it prints under `reports/consumed/` (a milestone keeps its own file name there, so a deferred one can still be merged later), and milestones arrive under `reports/milestones/` |

These are the substitutions the lead-proxy reference asks its caller for.

## Single-flight check

At most one harden pass per creation may be in flight, and an `update` and a
`heal` pass on the same creation count against each other. So before an update
or heal pass opens its ticket, run the pre-dispatch check in the
harden-contention reference ("Before dispatch: one pass per creation
(single-flight)") for every creation in `$CREATIONS`, and act on what it
finds:

- **No pass in flight on any of them**: dispatch.
- **Your own pass is live on any of them**: do not dispatch a second one. Note
  your new commits on your own ticket, as that reference says: the catch-up
  your own merge-time freshness check forces covers them.
- **Another agent's pass is live on any of them**: leave the note that
  reference describes on their ticket and stop. The catch-up forced at their
  merge time covers your change, so your turn-end obligation is met.
- **A pass's holder is gone but its worker is not**: take it over as its lead,
  as that reference says, instead of dispatching.
- **That reference had you supersede the passes that were in flight**:
  dispatch.

A crystallize pass does not run this check.

## The tracking ticket

One ticket tracks the pass from dispatch to go-live. Create the runtime dir,
create the ticket and start it, with your skill's command, before you write the
task file. For an update or heal pass two of its fields are read by other
chats:

- The **title** lists the creations (`<operation> $CREATIONS`), which is what
  the single-flight check matches on.
- The **description** names the worker (`Worker: <worker name>`), which is what
  that check's lookup of the pass's holder and worker needs.

Keep the ticket's id: you close it at the end of the pass ("Closing the
ticket"), not when the worker reports.

## The task file

The task file is the worker's whole brief. Its frontmatter carries the same
fields for every operation:

| Field | What it is |
|---|---|
| `finish_report_path` | The report the poll waits for. The worker delivers every gate and status report there. |
| `scope_file` | Where the worker writes the pass's computed footprint at the start of its pass. You name the path; the worker creates the file. |
| `diff_base` | The commit before the work being hardened. The scope file's diff is taken from it, and the worker's test gate selects its suites from what changed since it. Your skill says which commit that is for its operation. |
| `operation` | `crystallize`, `update` or `heal`. |
| `creations` | `$CREATIONS`. |

`launch` stamps the rest into the file (your agent id, your checkout, the task
file's own path, the worker's agent id), so you write none of it.

The body is your skill's template, filled in:

- Describe the work, the change or the failure so that it makes sense to
  someone who has not seen your transcript, and state the contract the creation
  must honor afterwards.
- **Anchor the worker's search with verbatim quotes** wherever it has to find
  something in your transcript: the user's request, the failing command or
  error, a tool output that defined the recipe. Without anchors the worker
  scans the wrong region of your transcript.
- Tell the worker to invoke the `harden-worker` skill, and, on a harness that
  cannot invoke skills, to read and follow
  `.agents/skills/harden-worker/SKILL.md` from its own checkout.
- Fill in the real content; do not leave a placeholder.

## Launching the worker

**Commit any pending changes before you launch, and never harden inline.** The
worker is created from your committed HEAD, so uncommitted changes never reach
it -- and `create_worker.py launch` refuses a dirty tree outright. Commit your
work first (the change being hardened is exactly what belongs on the branch);
**commit, never stash** -- stashed work gets lost during multi-agent
coordination. A dirty tree (even unrelated changes) is never a reason to do the
hardening inline: commit, then dispatch. Hardening always runs in the background
worker.

`launch` copies the runtime dir to the worker and sends it the task file. If
the frontmatter sets `source_artifacts_dir`, it copies that directory too, with
no extra flag.

## Polling and proxying

Arm the poll with your skill's `await` command, which starts it through
`system/scripts/workspace_bare_scripts/run_in_background.py` and returns at
once. Then end your turn; never wait on the worker any other way. The one
reason not to end the turn is work that is already yours to do: later steps of
a skill that invoked yours (`fetch-process-show` goes on to build its
surfaces), or something else the user asked for. Each report arrives in your
chat as a message that starts your next turn; handle it then.

You own this poll for as long as the worker runs, even if you reached this step
from another skill and moved on to other work: without it, gate reports never
reach the user and the worker deadlocks.

Handle every report as the lead-proxy reference says: the "do not interrupt
more recent user work" rule, gate decisions, milestones, terminal statuses, and
a poll that ends without a report. Re-arm the poll after every gate and every
milestone. Your skill lists the gates and terminal statuses its operation
produces. On `stuck`, follow the failure flow in
`.agents/skills/launch-task/references/worker-failure.md`, which stops the
worker and leaves the ticket open with a note.

The pass is non-blocking for the user: their request was already delivered
before you dispatched, and the worker produces a quieter follow-up commit.

## Before the merge

Before any merge from the worker's branch (the merge on `done`, or a
provisional milestone merge), run the three checks in the harden-contention
reference ("Before merge: lease, freshness, conflicts"), in order:

1. Wait out any foreground editing lease on an app or service in the pass.
2. Confirm the branch is still fresh: no creation's footprint has changed since
   the worker branched. A creation that is not on your branch yet (the skill a
   crystallize pass is building, before the first merge that brings it) is left
   out of this check, as that reference says.
3. Never resolve a conflicted merge yourself.

A stale or conflicted pass goes back to its worker to catch up to your `HEAD`
("Catching up a stale pass" there), and its next `done` comes back through
these same checks.

## Merge, then go live by type

Merge the worker's branch and destroy the worker as the lead-proxy reference
says for `done` ("Terminal status: act and stop polling"), which also has the
venv sync a merge that changed a `pyproject.toml` or `uv.lock` needs. Then go
live for each creation in the pass, by its type:

- **skill** or **shared**: nothing beyond the merge (the worker's
  cross-reference sweep is part of the change), except `uv sync --all-packages
  --frozen` when it has a `python/` project, so the workspace venv carries its
  package and any dependency the merge added. If it is a built-in skill, the
  change is local drift to reconcile later via `update-self` (pull) or
  `submit-upstream-changes` (push).
- **app**: refresh its window, so the user sees the merged build:

  ```bash
  uv run --no-sync workspace-layout refresh --app <app-name>
  ```

  For an app `build-app` made, run this instead, which reloads its mock frame
  while one is up and its own windows otherwise:

  ```bash
  uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py refresh --app <app-name>
  ```
- **service**: it has no window, so restart its programs (`supervisorctl
  restart <program>`): the one named after the package (`host_backup` runs as
  `host-backup`) and any `<program>-<role>` ones. `oom_priority`'s programs are
  named otherwise (`earlyoom`, `publish_cgroup_meminfo`, `resource_sampler`,
  and the `oom-tag-backstop` event listener): restart the ones the change
  reaches. A service with no program of its own (the Caretaker's check, which a
  routine runs) has nothing to restart: its next run reads the merged code.

## Closing the ticket

Close the ticket once every creation in the pass is live, with a summary that
names them (`tk close "$TICKET_ID" "<what was done to> $CREATIONS -- worker
branch merged."`). A pass that ends with nothing to merge (`no-update-needed`)
closes it too, after destroying the worker. A `stuck` pass leaves it open.
