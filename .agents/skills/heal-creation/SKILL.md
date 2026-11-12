---
name: heal-creation
description: "Fix an existing creation that errored or delivered a wrong result. This applies to skills, apps, and services. Invoke at turn-end, after the failure was worked around to satisfy the user's request."
metadata:
  author: imbue
---

# Healing a broken creation

This is the **heal** lead of the creation lifecycle. An existing creation
should have delivered the correct result but did not; you dispatch a harden
worker to reproduce the incident, find the root cause, apply a minimal fix,
re-run scenarios, and present a single approval gate.

Standing instructions:

- **Heal at turn-end.** Do not interrupt in-flight work to invoke it: the
  user's original request is already delivered, with the failure worked
  around, and the heal worker produces a quieter follow-up commit.
- **One incident, one pass.** A heal pass covers every creation the failure
  spans, with one worker: when an app broke because of how it calls a skill, or
  two apps disagree about what they exchange, heal them together rather than
  dispatching a pass per creation. The fix often has to change both sides, and
  two workers would each see only half of the failure.
- **Never fix inline.** Healing always runs in the background worker. Commit
  pending changes, never stash them, then dispatch.
- **After a compaction**, re-read the shared references below before you handle
  the worker's next report.

Three shared references carry the detail, and each step names the section it
needs:

- `.agents/shared/references/harden-lead.md`: the dispatch steps this skill
  shares with `update-creation` and `crystallize-creation`.
- `.agents/shared/references/harden-contention.md`: one pass per creation, and
  the checks before a merge.
- `.agents/shared/references/lead-proxy.md`: polling, answering gates, acting
  on the worker's terminal status.

## Steps

- When NOT to heal; Names, and a critical app: settle these first
- Step 1: Open a tracking ticket, after the single-flight check
- Step 2: Write the task file
- Step 3: Launch the worker and poll (end your turn each time the poll is
  armed)
- Step 4: Merge and go live, on `done`

## When NOT to heal

- The creation worked fine; the request was genuinely out of its scope -- that
  is an `update-creation` situation, not a heal.
- The failure was one-off and transient (network hiccup, rate limit).
- You are unsure why it failed. Finish the user's request, gather evidence, then
  decide if heal applies.

## Names, and a critical app

`$CREATIONS` lists every creation the pass heals as `<type>:<name>`, and
`$SLUG` names the pass: the creation's own name when there is one, otherwise a
short kebab-case name for the failure (`roadmap-sync`). `harden-lead.md`
("Names and paths") has the format and every path that follows from them:

- Worker and branch: `heal-$SLUG` / `mngr/heal-$SLUG`
- Runtime dir and task file: `data/.tasks/harden/heal-$SLUG/` and
  `data/.tasks/harden/heal-$SLUG/task.md`

A heal of a critical app (the shell, the chat, the terminal, or an app whose
manifest says `critical = true`) is a live edit of the served tree at your own
risk: there is no preview, no kept rollback point, and no notice behind it, so
a wrong fix takes the user's surface down with nothing to undo it but your next
edit. The safe path is `update-app`'s careful flow
(`.agents/skills/update-app/references/critical-app.md`); take it whenever the
fix can wait for a preview.

## Step 1: Open a tracking ticket

Run the single-flight check first, for every creation in `$CREATIONS`: the
command is in `.agents/shared/references/harden-contention.md` ("Before
dispatch: one pass per creation (single-flight)"), and `harden-lead.md`
("Single-flight check") says what each outcome means for you. Dispatch only if
no pass is in flight on any of them, or that reference had you supersede the
ones that were.

Then open the ticket. Its title and description are read by other chats'
single-flight checks (`harden-lead.md`, "The tracking ticket"):

```bash
mkdir -p data/.tasks/harden/heal-$SLUG
TICKET_ID=$(tk create "heal $CREATIONS" -t bug -d "Worker: heal-$SLUG" \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Write the task file

`harden-lead.md` ("The task file") explains the frontmatter fields and the
rules for the body. For a heal, `diff_base` is your `HEAD` at dispatch: the fix
does not exist yet, so the worker's own commits are the whole diff. The body
describes the failure and anchors the worker's search with verbatim quotes (the
user's request, the failing command or error, any tool output that exposed the
misbehavior).

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
Invoke the \`harden-worker\` skill. On a harness that cannot invoke skills, read
and follow \`.agents/skills/harden-worker/SKILL.md\` from your own checkout. It
reads \`operation\` and \`creations\` from this frontmatter and follows the
matching references: reproduce the failure, find the root cause, apply a minimal fix,
re-run 2-3 fresh scenarios, and present the single final-creation gate.
Report to the lead per its reporting protocol.

## Success criteria
- The incident reproduces against the current creations before the fix.
- The fix addresses the root cause, not a symptom.
- The fresh scenarios pass after the fix.
- The user approved the final creation (via a delivered final-creation gate report).
TASK_EOF
```

Fill in the real content; do not leave placeholders.

## Step 3: Launch the worker and poll

Commit any pending changes first; a dirty tree, even from unrelated changes, is
never a reason to do the fix inline (`harden-lead.md`, "Launching the worker").

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py launch \
    --name heal-$SLUG \
    --template worker \
    --runtime-dir data/.tasks/harden/heal-$SLUG/ \
    --task-file data/.tasks/harden/heal-$SLUG/task.md
```

Arm the poll, then end your turn:

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the background agent" --waiter -- \
    uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py await \
    --name heal-$SLUG \
    --task-file data/.tasks/harden/heal-$SLUG/task.md \
    --timeout 90m
```

Handle each report as `.agents/shared/references/lead-proxy.md` says, and
re-arm this poll after every gate. For a heal:

- The only user-approval gate is `final-creation` -- a heal has no outline
  gate. A `question` gate can arrive at any point.
- Terminal statuses: `done` (Step 4); `stuck` (the failure flow in
  `.agents/skills/launch-task/references/worker-failure.md`, which stops the
  worker).

## Step 4: Merge and go live

On `done`:

1. Run the checks before a merge, in
   `.agents/shared/references/harden-contention.md` ("Before merge: lease,
   freshness, conflicts"): wait out any foreground editing lease on an app or
   service in the pass, confirm the branch is still fresh, and never resolve a
   conflicted merge yourself. A stale or conflicted pass goes back to its
   worker to catch up to your `HEAD`, and its next `done` comes back through
   these same checks.
2. Merge `mngr/heal-$SLUG` and destroy the worker (`create_worker.py destroy
   --name heal-$SLUG`), as `lead-proxy.md` says for `done` ("Terminal status:
   act and stop polling").
3. Go live for each creation in the pass by its type: `harden-lead.md`,
   "Merge, then go live by type".
4. Close the ticket:

   ```bash
   tk close "$TICKET_ID" "Healed $CREATIONS -- worker branch merged."
   ```
