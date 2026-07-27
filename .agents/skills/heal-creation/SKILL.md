---
name: heal-creation
description: "Fix an existing creation that errored or delivered a wrong result. This applies to skills, apps, and services. Invoke at turn-end, after you worked around the failure to satisfy the user's request."
---

# Healing a broken creation

This is the **heal** lead of the generic creation lifecycle. An existing
creation should have delivered the correct result but did not; you dispatch a
generic worker to reproduce the incident, find the root cause, apply a minimal
fix, re-run scenarios, and present a single approval gate. Heal is a turn-end
action -- do not interrupt in-flight work to invoke it; the user's original
request is already delivered.

## The type parameter

`type` is `skill` (the default), `app`, or `service`. The worker reads it and loads
`type-<TYPE>.md`. (A system-interface regression is a heal *operation*
too, but it is driven through `update-system-interface`, which owns the
`safe-reveal` preview/reveal/rollback go-live -- do not drive a system-interface
heal from here.)

## When NOT to heal

- The creation worked fine; the request was genuinely out of its scope -- that
  is an `update-creation` situation, not a heal.
- The failure was one-off and transient (network hiccup, rate limit).
- You are unsure why it failed. Finish the user's request, gather evidence, then
  decide if heal applies.

## Conventions

Use `$TARGET` for the creation you are healing (e.g. `migrate-config`, an app
or service name). Then:

- Worker agent name and branch: `heal-$TARGET` / `mngr/heal-$TARGET`
- Runtime dir / task file: `data/.tasks/harden/heal-$TARGET/` /
  `data/.tasks/harden/heal-$TARGET/task.md`

## Step 1: Open a tracking ticket

**Single-flight check first.** At most one harden pass per creation may be in
flight (counting `update` passes on the same target). Run the pre-dispatch
check in [`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md);
if another agent's pass is live, leave the note it describes on their ticket
and stop -- the superseding pass forced at their merge time covers your fix.
Only dispatch if no pass is live (or you took over an abandoned one).

```bash
mkdir -p data/.tasks/harden/heal-$TARGET
TICKET_ID=$(tk create "heal $TARGET" -t bug \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Write the task file

Frontmatter carries `operation: heal`, the `type`, and the worker reporting
fields (per `.agents/shared/references/worker-reporting.md`). The body describes
the failure and anchors the worker's search with verbatim quotes (the user's
request, the failing command or error, any tool output that exposed the
misbehavior). Without anchors the worker scans the wrong region of your
transcript.

```bash
cat > data/.tasks/harden/heal-$TARGET/task.md << TASK_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: data/.tasks/harden/heal-$TARGET/reports/report.md
operation: heal
type: skill
---

# Task: heal \`$TARGET\`

## Incident summary
<2-5 sentences: what the user asked for, how \`$TARGET\` was invoked, how it
failed, what you did to work around it.>

## Anchors (verbatim quotes)
The worker uses these with \`mngr transcript\` to locate the incident. Include
the user's request that invoked \`$TARGET\` (verbatim), the failing output /
exception / wrong result (verbatim), and any clarifying quote about expected
behavior.
<paste quotes here, one per bullet.>

## What the fixed creation must do
<the contract the healed creation must honor -- what input shapes should work,
what outputs are correct. Describe success; the incident itself is above.>

## What to do
Use the installed \`harden-worker\` sub-skill. It reads \`operation\` and
\`type\` from this frontmatter and follows the matching references:
reproduce the failure, find the root cause, apply a minimal fix, re-run 2-3
fresh scenarios, and push through the single final-creation gate. Push reports
to the lead per its reporting protocol.

## Success criteria
- The incident reproduces against the current creation before the fix.
- The fix addresses the root cause, not a symptom.
- The fresh scenarios pass after the fix.
- The user approved the final creation (via a pushed final-creation gate report).
TASK_EOF
```

Set `type:` as appropriate and fill in the real content; do not leave
placeholders.

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
    --name heal-$TARGET \
    --template subskill-worker \
    --runtime-dir data/.tasks/harden/heal-$TARGET/ \
    --task-file data/.tasks/harden/heal-$TARGET/task.md
```

Then background-poll (`create_worker.py await --task-file ... --timeout 90m`,
`run_in_background: true`) and follow `.agents/shared/references/lead-proxy.md`.
Flow-specific substitutions:

- Worker name: `heal-$TARGET`; branch: `mngr/heal-$TARGET`
- Poll path: `data/.tasks/harden/heal-$TARGET/reports/report.md`; reports dir
  `data/.tasks/harden/heal-$TARGET/reports/`; consumed
  `data/.tasks/harden/heal-$TARGET/reports/consumed/`
- The only user-approval gate is `final-creation` -- a heal has no outline gate.
- Terminal statuses: `done` (go live, Step 4); `stuck` (failure flow per
  `.agents/skills/launch-task/references/worker-failure.md`).

## Step 4: Merge and go live

On `done`, first run the merge-time checks in
[`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md):
wait out any foreground editing lease on the service, confirm the branch is
still fresh (the creation's footprint has not changed since the worker
branched), and never hand-resolve a conflicted merge -- a stale or conflicted
pass is discarded and superseded by one new pass covering everything since the
last hardened merge.

Then merge `mngr/heal-$TARGET` and go live by type: a **skill** needs
nothing beyond the merge; an **app** wants a tab refresh (`python3
system/scripts/layout.py refresh <app-name>`); a background **service** has no
tab -- restart it (`supervisorctl restart <name>`) instead. Then close the
ticket:

```bash
tk close "$TICKET_ID" "Healed $TARGET -- worker branch merged."
```

## Gotchas

- If the target is a built-in upstream skill, healing it causes local drift to
  reconcile later via `update-self` (pull) or `submit-upstream-changes` (push).
- Heal is non-blocking -- the user's original request is already delivered; the
  heal worker produces a quieter follow-up commit.
