---
name: update-creation
description: "Change an existing skill, app, service, or shared script/reference (under .agents/shared/) -- extend it, refactor it, or just verify it still works. Invoke at turn-end when a skill ran but you had to do extra repeatable work by hand, or when you and the user discussed a change to it and applied it live."
metadata:
  author: imbue
---

# Updating an existing creation

This is the **change** lead of the generic creation lifecycle. The creation
already exists; you dispatch a generic worker to harden the change in the
background, proxy its gates, merge, and go live. There is one flow with a
**design-gate toggle** keyed on whether the change is already committed.

## One change, one pass

A pass hardens a *change*, not a creation. Its scope is what this chat
changed, or (for the emergent origin) every creation the gap spans, and one
worker covers all of it. Two apps edited so they work together are one pass;
so are an app and the skill that drives it, or two unrelated tweaks made in
the same turn. Never dispatch a pass per creation. Every worker pays its own
environment setup before it does any work, and two passes over one change
would each verify half of something that only works whole. When a change holds
parts that are separate jobs, the worker splits them across its own sub-workers
(`.agents/shared/worker/references/harden-creation.md`, "Splitting the pass
across sub-workers"); that is its call, not yours.

## The two origins

- **committed** -- you and the user discussed the change live and it is already
  committed on the branch. The design was approved organically in chat, so the
  worker skips the design gate, reads the committed diff as ground truth,
  verifies it, and presents the final gate.
- **emergent** -- a skill ran but additional *repeatable* work had to be done by
  hand, with no design conversation. The worker reconstructs the incident from
  your transcript, runs a design gate (Gate 1), implements, runs scenarios, and
  presents the final gate.

Pick **committed** when the user was explicitly in the design loop *and* the
change is already committed. Otherwise **emergent** (the safe default; its
Gate 1 re-surfaces the design for an approval pass).

**Confirm the user actually wants the change before you dispatch (committed
origin).** Applying a change live is not the user approving it. Do not launch
the hardening worker in the same turn you made the change: end that turn by
surfacing the change and asking whether it's what they wanted, and only invoke
this flow once they've given a clear go-ahead. Like any incremental work this
can take **several rounds** -- the user may keep responding with adjustments;
treat each as another live iteration and do not launch the worker until you
are sure they're satisfied with the update, not just at a single mid-thread
"looks fine." (The emergent origin is transparent work with no live change to
approve, so this gate doesn't apply there; its Gate 1 covers design approval.)

"Repeatable" covers deterministic extensions (an extra flag, a new output
format), model-judgement extensions (an additional judgement step with a stable
recipe, scripted as `[ai-script]`), and executor meta-work. One-off creative or
exploratory work is NOT an update candidate.

## The type parameter

Each creation in the pass has a type: `skill`, `app`, `service`, or `shared` (a
shared script or reference, handled as a skill). It drives where the worker
looks (`type-<TYPE>.md`) and that creation's **go-live** (Step 4): skill or
shared → cross-reference sweep is part of the edit, nothing else; app → refresh
the window (a background service has no window -- restart it instead). A
critical app (the shell, the chat, the terminal, or an app whose manifest says
`critical = true`) is an `app:` creation too, but it reaches this flow only from
`update-app`'s careful flow (`references/critical-app.md`), which calls into
Steps 1-3 for the orchestration core, creates the worker at approval on the
branch the lead already built up, and owns the go-live through the atomic update
apply. When a change touches a critical app, the **whole change** takes that
flow: every other creation it touched is edited on the same branch, rides the
same pass, and lands through the same apply.

## Conventions

Use `$CREATIONS` for every creation the pass covers, each as `<type>:<name>`,
space-separated: `app:<registered name>`, `skill:<directory>`,
`service:<package>`, or `shared:<path under .agents/shared>` for a shared
script or reference (e.g. `skill:migrate-config`, or `app:roadmap
skill:sync-roadmap` for an app and the skill that drives it). Use `$SLUG` for
the pass: the creation's own name when there is one, otherwise a short
kebab-case name for the change (`roadmap-sync`). Then:

- Worker agent name and branch: `update-$SLUG` / `mngr/update-$SLUG`
- Runtime dir / task file: `data/.tasks/harden/update-$SLUG/` /
  `data/.tasks/harden/update-$SLUG/task.md`

## Step 1: Open a tracking ticket

**Single-flight check first.** At most one harden pass per creation may be in
flight (counting `heal` passes on the same target). Run the pre-dispatch check
in [`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md)
for every creation in `$CREATIONS`; if another agent's pass is live on any of
them, leave the note it describes on their ticket and stop -- the catch-up
forced at their merge time covers your change. If a pass's holder is gone but
its worker is not, take it over as its lead there instead of dispatching. Only
dispatch if no pass is in flight on any of them, or that reference had you
supersede the ones that were.

The ticket's title lists the creations, which is what that check matches on,
and its description names the worker, which is what the check's lookup of its
holder and worker needs:

```bash
mkdir -p data/.tasks/harden/update-$SLUG
TICKET_ID=$(tk create "update $CREATIONS" -t task -d "Worker: update-$SLUG" \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Capture the change and write the task file

For the **committed** origin, capture the commit metadata and full diff so the
worker has a convenience index (the change is also on its branch on disk):

```bash
COMMIT_RANGE="HEAD~1..HEAD"   # widen to cover all commits implementing the change
git log --format='%H %s' "$COMMIT_RANGE" > data/.tasks/harden/update-$SLUG/commit.log
git log -p "$COMMIT_RANGE"    > data/.tasks/harden/update-$SLUG/commit.diff
DIFF_BASE_REF="${COMMIT_RANGE%%..*}"   # the commit before the change
```

For the **emergent** origin there is no committed change yet, so the diff base
is your current commit:

```bash
DIFF_BASE_REF=HEAD
```

Write the task file. Frontmatter carries `operation: update`, `creations`
(`$CREATIONS`), `finish_report_path` (the report destination the lead polls;
see `.agents/shared/references/worker-reporting.md`), `scope_file` (where the
worker writes the pass's computed footprint at the start of its run -- you name
the path, the worker creates the file), and `diff_base` (the commit before the
work being hardened: for the committed origin, the start of `$COMMIT_RANGE`, so
the scope file's diff covers the committed change; for the emergent origin,
your `HEAD` at dispatch). The worker's test gate also selects its suites from
what changed since `diff_base`. The body carries the `## Change origin` marker
the worker dispatches on, plus origin-specific content:

```bash
cat > data/.tasks/harden/update-$SLUG/task.md << TASK_EOF
---
finish_report_path: data/.tasks/harden/update-$SLUG/reports/report.md
scope_file: data/.tasks/harden/update-$SLUG/scope.json
diff_base: $(git rev-parse "$DIFF_BASE_REF")
operation: update
creations: $CREATIONS
---

# Task: update \`$CREATIONS\`

## Change origin
ORIGIN: emergent

## Incident summary (emergent) / Committed change (committed)
<emergent: 2-5 sentences -- what the user asked for, how the creations fell
short, and the additional repeatable work you did by hand.>
<committed: branch, commit range (see commit.log), a summary of what changed and
why, and the design rationale the worker checks the diff against.>

## Anchors (verbatim quotes)
The worker uses these with \`mngr transcript\` to locate the relevant turns.
<emergent: the user's request, the insufficient output, and a quote
showing the manual follow-up. committed: 1-3 quotes that pinned the design.>

## What the updated creations must do
<emergent only: the contract each creation must honor after the change -- inputs
it should now accept, outputs it should now produce, and what the creations
now exchange with each other. Describe the new contract; the incident is
captured above.>

## What to do
Read and follow \`.agents/shared/worker/SKILL.md\` from your own checkout. It
reads \`operation\`, \`creations\`, and the \`## Change origin\` marker, then
follows the matching references. Push reports to the lead per its reporting protocol.

## Success criteria
- The change is hardened, tested, and passes the review gates on your branch.
- The user approved the final creation (Gate 2); for the emergent origin, also
  the outline (Gate 1).
TASK_EOF
```

Set `ORIGIN: committed` as appropriate. Fill in the real content; do not leave
placeholders. The `## Change origin` marker is required --
the worker fails loudly if it is missing.

**One exception, and it lives here so you meet it where the format is defined:**
a harden task dispatched from the careful flow for a critical app
(`update-app/references/critical-app.md`) carries **no `## Change origin` marker
and no worker gate** (see the critical-app handoff in
`.agents/shared/worker/references/op-update.md`). That flow already got the
user's approval through its live preview loop, so re-arming the gate would ask
them to approve the same thing twice -- and writing the marker anyway, or
telling the worker in the task body to follow it, puts the gate back. Every
other task file needs it.

## Step 3: Launch the worker and poll

**Commit any pending changes before you launch, and never harden inline.** The
worker is created from your committed HEAD, so uncommitted changes never reach
it -- and `create_worker.py launch` refuses a dirty tree outright. Commit your
work first (the change being hardened is exactly what belongs on the branch);
**commit, never stash** -- stashed work gets lost during multi-agent
coordination. A dirty tree (even unrelated changes) is never a reason to do the
hardening inline: commit, then dispatch. Hardening always runs in the background
worker.

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-$SLUG \
    --template worker \
    --runtime-dir data/.tasks/harden/update-$SLUG/ \
    --task-file data/.tasks/harden/update-$SLUG/task.md
```

Then background-poll (`create_worker.py await --name update-$SLUG --task-file
... --timeout 90m`, started through `system/scripts/run_in_background.py`) and
follow `.agents/shared/references/lead-proxy.md`.
Flow-specific substitutions:

- Worker name: `update-$SLUG`; branch: `mngr/update-$SLUG`
- Poll path: `data/.tasks/harden/update-$SLUG/reports/report.md`; reports dir
  `data/.tasks/harden/update-$SLUG/reports/`; `await` archives each report it
  prints under `data/.tasks/harden/update-$SLUG/reports/consumed/`
- Gates: `outline-approval` (emergent only -- the design gate) and
  `final-creation` (both).
- Terminal statuses: `done` (go live, Step 4); `no-update-needed` (no change --
  close the ticket, destroy the worker per `lead-proxy.md`, no merge); `stuck`
  (failure flow per `.agents/skills/launch-task/references/worker-failure.md`,
  which stops the worker).

## Step 4: Merge and go live

On `done`, first run the merge-time checks in
[`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md):
wait out any foreground editing lease on an app or service in the pass,
confirm the branch is still fresh (no creation's footprint has changed since
the worker branched), and never resolve a conflicted merge yourself -- a stale
or conflicted pass goes back to its worker to catch up to your `HEAD`, and its
next `done` comes back through these same checks.

Then merge `mngr/update-$SLUG`, destroy the worker per `lead-proxy.md`
(`create_worker.py destroy --name update-$SLUG`), and go live for each creation
in the pass, by its type:

- **skill** or **shared**: nothing beyond the merge (the worker's
  cross-reference sweep is part of the change). If it is a built-in upstream
  skill, note the local drift to reconcile later via `update-self` /
  `submit-upstream-changes`.
- **app**: refresh its window (`uv run --no-sync workspace-layout refresh
  --app <app-name>`).
- **service**: it has no window -- restart its programs (`supervisorctl restart
  <program>`): the one named after the package (`host_backup` runs as
  `host-backup`) and any `<program>-<role>` ones. `oom_priority`'s programs
  are named otherwise (`earlyoom`, `publish_cgroup_meminfo`,
  `resource_sampler`, and the `oom-tag-backstop` event listener): restart the
  ones the change reaches. A service with no
  program of its own (the Caretaker's check, which a routine runs) has nothing
  to restart: its next run reads the merged code.
- **a critical app**: do **not** merge or go live here -- the careful flow
  (`update-app/references/critical-app.md`) drives the live preview loop and
  the go-live through the atomic update apply. (It uses this flow for Steps 1-3
  only, and launches the worker with a `--branch` passthrough onto the branch
  the lead already built up.)

Then close the ticket:

```bash
tk close "$TICKET_ID" "Updated $CREATIONS -- worker branch merged."
```

## Gotchas

- Update is non-blocking -- the user's original request is already delivered;
  the update worker produces a quieter follow-up commit.
- The committed origin's worker may produce no new commits of its own if
  verification is clean. That is expected -- the substantive change is already
  on the branch from the live commit.
