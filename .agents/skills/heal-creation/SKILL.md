---
name: heal-creation
description: "Fix an existing creation that errored or delivered a wrong result. This applies to skills, apps, and services. Invoke at turn-end, after you worked around the failure to satisfy the user's request."
metadata:
  author: imbue
---

# Healing a broken creation

This is the **heal** lead of the generic creation lifecycle. An existing
creation should have delivered the correct result but did not; you dispatch a
generic worker to reproduce the incident, find the root cause, apply a minimal
fix, re-run scenarios, and present a single approval gate. Heal is a turn-end
action -- do not interrupt in-flight work to invoke it; the user's original
request is already delivered.

**One incident, one pass.** A heal pass covers every creation the failure
spans, with one worker: when an app broke because of how it calls a skill, or
two apps disagree about what they exchange, heal them together rather than
dispatching a pass per creation. The fix often has to change both sides, and
two workers would each see only half of the failure.

## The type parameter

Each creation in the pass has a type: `skill`, `app`, `service`, or `shared` (a
shared script or reference, handled as a skill). The worker reads it and loads
`type-<TYPE>.md`. A heal of a critical app (the shell, the chat, the terminal,
or an app whose manifest says `critical = true`) is a live edit of the served
tree at your own risk: there is no preview, no kept rollback point, and no
notice behind it, so a wrong fix takes the user's surface down with nothing to
undo it but your next edit. The safe path is `update-app`'s careful flow
(`references/critical-app.md`); take it whenever the fix can wait for a preview.

## When NOT to heal

- The creation worked fine; the request was genuinely out of its scope -- that
  is an `update-creation` situation, not a heal.
- The failure was one-off and transient (network hiccup, rate limit).
- You are unsure why it failed. Finish the user's request, gather evidence, then
  decide if heal applies.

## Conventions

Use `$CREATIONS` for every creation the pass heals, each as `<type>:<name>`,
space-separated: `app:<registered name>`, `skill:<directory>`,
`service:<package>`, or `shared:<path under .agents/shared>` for a shared
script or reference (e.g. `skill:migrate-config`, or `app:roadmap
skill:sync-roadmap`). Use `$SLUG` for the pass: the creation's own name when
there is one, otherwise a short kebab-case name for the failure
(`roadmap-sync`). Then:

- Worker agent name and branch: `heal-$SLUG` / `mngr/heal-$SLUG`
- Runtime dir / task file: `data/.tasks/harden/heal-$SLUG/` /
  `data/.tasks/harden/heal-$SLUG/task.md`

## Step 1: Open a tracking ticket

**Single-flight check first.** At most one harden pass per creation may be in
flight (counting `update` passes on the same target). Run the pre-dispatch
check in [`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md)
for every creation in `$CREATIONS`; if another agent's pass is live on any of
them, leave the note it describes on their ticket and stop -- the catch-up
forced at their merge time covers your fix. If a pass's holder is gone but
its worker is not, take it over as its lead there instead of dispatching. Only
dispatch if no pass is in flight on any of them, or that reference had you
supersede the ones that were.

The ticket's title lists the creations, which is what that check matches on,
and its description names the worker, which is what the check's lookup of its
holder and worker needs:

```bash
mkdir -p data/.tasks/harden/heal-$SLUG
TICKET_ID=$(tk create "heal $CREATIONS" -t bug -d "Worker: heal-$SLUG" \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Write the task file

Frontmatter carries `operation: heal`, `creations` (`$CREATIONS`), the worker
reporting fields (per `.agents/shared/references/worker-reporting.md`),
`scope_file` (where the worker writes the pass's computed footprint at the start
of its run -- you name the path, the worker creates the file), and `diff_base`
(your `HEAD` at dispatch: the fix does not exist yet, so the worker's own
commits are the whole diff). The worker's test gate also selects its suites from
what changed since `diff_base`. The body describes the failure and anchors the
worker's search with verbatim quotes (the user's request, the failing command or
error, any tool output that exposed the misbehavior). Without anchors the worker
scans the wrong region of your transcript.

```bash
cat > data/.tasks/harden/heal-$SLUG/task.md << TASK_EOF
---
finish_report_path: data/.tasks/harden/heal-$SLUG/reports/report.md
scope_file: data/.tasks/harden/heal-$SLUG/scope.json
diff_base: $(git rev-parse HEAD)
operation: heal
creations: $CREATIONS
---

# Task: heal \`$CREATIONS\`

## Incident summary
<2-5 sentences: what the user asked for, how the creations were invoked, how
they failed, what you did to work around it.>

## Anchors (verbatim quotes)
The worker uses these with \`mngr transcript\` to locate the incident. Include
the user's request that invoked them (verbatim), the failing output /
exception / wrong result (verbatim), and any clarifying quote about expected
behavior.
<paste quotes here, one per bullet.>

## What the fixed creations must do
<the contract each healed creation must honor -- what input shapes should work,
what outputs are correct. Describe success; the incident itself is above.>

## What to do
Read and follow \`.agents/shared/worker/SKILL.md\` from your own checkout. It
reads \`operation\` and \`creations\` from this frontmatter and follows the
matching references: reproduce the failure, find the root cause, apply a minimal fix,
re-run 2-3 fresh scenarios, and push through the single final-creation gate.
Push reports to the lead per its reporting protocol.

## Success criteria
- The incident reproduces against the current creations before the fix.
- The fix addresses the root cause, not a symptom.
- The fresh scenarios pass after the fix.
- The user approved the final creation (via a pushed final-creation gate report).
TASK_EOF
```

Fill in the real content; do not leave placeholders.

## Step 3: Launch the worker and poll

**Commit any pending changes before you launch, and never harden inline.** The
worker is created from your committed HEAD, so uncommitted changes never reach
it -- and `create_worker.py launch` refuses a dirty tree outright. Commit your
work first; **commit, never stash** -- stashed work gets lost during
multi-agent coordination. A dirty tree (even unrelated changes) is never a
reason to do the fix inline: commit, then dispatch. Healing always runs in the
background worker.

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name heal-$SLUG \
    --template worker \
    --runtime-dir data/.tasks/harden/heal-$SLUG/ \
    --task-file data/.tasks/harden/heal-$SLUG/task.md
```

Then background-poll (`create_worker.py await --name heal-$SLUG --task-file ...
--timeout 90m`, started through `system/scripts/run_in_background.py`) and follow
`.agents/shared/references/lead-proxy.md`.
Flow-specific substitutions:

- Worker name: `heal-$SLUG`; branch: `mngr/heal-$SLUG`
- Poll path: `data/.tasks/harden/heal-$SLUG/reports/report.md`; reports dir
  `data/.tasks/harden/heal-$SLUG/reports/`; `await` archives each report it
  prints under `data/.tasks/harden/heal-$SLUG/reports/consumed/`
- The only user-approval gate is `final-creation` -- a heal has no outline gate.
- Terminal statuses: `done` (go live, Step 4); `stuck` (failure flow per
  `.agents/skills/launch-task/references/worker-failure.md`, which stops the
  worker).

## Step 4: Merge and go live

On `done`, first run the merge-time checks in
[`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md):
wait out any foreground editing lease on an app or service in the pass,
confirm the branch is still fresh (no creation's footprint has changed since
the worker branched), and never resolve a conflicted merge yourself -- a stale
or conflicted pass goes back to its worker to catch up to your `HEAD`, and its
next `done` comes back through these same checks.

Then merge `mngr/heal-$SLUG`, destroy the worker per `lead-proxy.md`
(`create_worker.py destroy --name heal-$SLUG`), and go live for each creation in
the pass, by its type: a **skill** or **shared** creation needs nothing beyond
the merge; an **app** wants a window refresh (`uv run --no-sync
workspace-layout refresh --app <app-name>`); a background **service** has no
window -- restart its programs (`supervisorctl restart <program>`) instead: the
one named after the package (`host_backup` runs as `host-backup`) and any
`<program>-<role>` ones (`oom_priority`'s are named otherwise: restart those of
`earlyoom`, `publish_cgroup_meminfo`, `resource_sampler`, and the
`oom-tag-backstop` event listener the fix reaches);
one with no program of its own (the Caretaker's check, which a routine runs)
has nothing to restart, since its next run reads the merged code. Then close
the ticket:

```bash
tk close "$TICKET_ID" "Healed $CREATIONS -- worker branch merged."
```

## Gotchas

- If a creation in the pass is a built-in upstream skill, healing it causes local drift to
  reconcile later via `update-self` (pull) or `submit-upstream-changes` (push).
- Heal is non-blocking -- the user's original request is already delivered; the
  heal worker produces a quieter follow-up commit.
