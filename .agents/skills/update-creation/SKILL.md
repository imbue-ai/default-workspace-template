---
name: update-creation
description: "Change an existing skill, app, service, or shared script/reference -- extend it, refactor it, or just verify it still works. Invoke at turn-end when a skill ran but extra repeatable work had to be done by hand, or when a change to it was discussed with the user and applied live."
metadata:
  author: imbue
---

# Updating an existing creation

This is the **change** lead of the creation lifecycle. The creation already
exists; you dispatch a harden worker to harden the change in the background,
proxy its gates, merge, and go live. There is one flow with a **design-gate
toggle** keyed on whether the change is already committed.

Standing instructions:

- **Update at turn-end.** The user's original request is already delivered;
  the update worker produces a quieter follow-up commit.
- **One change, one pass, one worker** ("One change, one pass" below).
- **Never harden inline.** Hardening always runs in the background worker.
  Commit pending changes, never stash them, then dispatch.
- **A change that touches a critical app never starts here.** A critical app
  (the shell, the chat, the terminal, or an app whose manifest says `critical =
  true`) is an `app:` creation too, but the **whole change** takes
  `update-app`'s careful flow
  (`.agents/skills/update-app/references/critical-app.md`): every other
  creation it touched is edited on the same branch, rides the same pass, and
  lands through the same apply. That flow calls into Steps 1-3 here and owns
  the go-live. When it sent you here, read
  `references/critical-app-handoff.md` before Step 1 for what differs.
- **After a compaction**, re-read the shared references below before you handle
  the worker's next report.

Three shared references carry the detail, and each step names the section it
needs:

- `.agents/shared/references/harden-lead.md`: the dispatch steps this skill
  shares with `heal-creation` and `crystallize-creation`.
- `.agents/shared/references/harden-contention.md`: one pass per creation, and
  the checks before a merge.
- `.agents/shared/references/lead-proxy.md`: polling, answering gates, acting
  on the worker's terminal status.

## Steps

- Before Step 1, settle the pass's scope (One change, one pass), its origin
  (The two origins) and its Names
- Step 1: Open a tracking ticket, after the single-flight check
- Step 2: Capture the change and write the task file
- Step 3: Launch the worker and poll (end your turn each time the poll is
  armed)
- Step 4: Merge and go live, on `done`

## One change, one pass

A pass hardens a *change*, not a creation. Its scope is what this chat
changed, or (for the emergent origin) every creation the gap spans, and one
worker covers all of it. Two apps edited so they work together are one pass;
so are an app and the skill that drives it, or two unrelated tweaks made in
the same turn. Never dispatch a pass per creation. Every worker pays its own
environment setup before it does any work, and two passes over one change
would each verify half of something that only works whole. When a change holds
parts that are separate jobs, the worker splits them across workers of its own
(the harden worker's contract, "Splitting the pass across workers of your
own"); that is its call, not yours.

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

## Names

`$CREATIONS` lists every creation the pass covers as `<type>:<name>`, and
`$SLUG` names the pass: the creation's own name when there is one, otherwise a
short kebab-case name for the change (`roadmap-sync`). `harden-lead.md`
("Names and paths") has the format and every path that follows from them:

- Worker and branch: `update-$SLUG` / `mngr/update-$SLUG`
- Runtime dir and task file: `data/.tasks/harden/update-$SLUG/` and
  `data/.tasks/harden/update-$SLUG/task.md`

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

Write the task file. `harden-lead.md` ("The task file") explains the
frontmatter fields and the rules for the body. For an update, `diff_base` is
the commit before the work being hardened: for the committed origin, the start
of `$COMMIT_RANGE`, so the scope file's diff covers the committed change; for
the emergent origin, your `HEAD` at dispatch. The body carries the `## Change
origin` marker the worker dispatches on, plus origin-specific content:

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
Invoke the \`harden-worker\` skill. On a harness that cannot invoke skills, read
and follow \`.agents/skills/harden-worker/SKILL.md\` from your own checkout. It
reads \`operation\`, \`creations\`, and the \`## Change origin\` marker, then
follows the matching references. Report to the lead per its reporting protocol.

## Success criteria
- The change is hardened, tested, and passes the review gates on your branch.
- The user approved the final creation (Gate 2); for the emergent origin, also
  the outline (Gate 1).
TASK_EOF
```

Set `ORIGIN: committed` as appropriate. Fill in the real content; do not leave
placeholders. The `## Change origin` marker is required -- the worker fails
loudly if it is missing. The one task file that carries no marker is the
careful flow's handoff for a critical app
(`references/critical-app-handoff.md`).

## Step 3: Launch the worker and poll

Commit any pending changes first (the change being hardened is exactly what
belongs on the branch); a dirty tree, even from unrelated changes, is never a
reason to do the hardening inline (`harden-lead.md`, "Launching the worker").

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-$SLUG \
    --template worker \
    --runtime-dir data/.tasks/harden/update-$SLUG/ \
    --task-file data/.tasks/harden/update-$SLUG/task.md
```

Arm the poll, then end your turn:

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the background agent" --waiter -- \
    uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py await \
    --name update-$SLUG \
    --task-file data/.tasks/harden/update-$SLUG/task.md \
    --timeout 90m
```

Handle each report as `.agents/shared/references/lead-proxy.md` says, and
re-arm this poll after every gate. For an update:

- Gates: `outline-approval` (emergent only -- the design gate) and
  `final-creation` (both). A `question` gate can arrive at any point.
- Terminal statuses: `done` (Step 4); `no-update-needed` (no change -- destroy
  the worker per `lead-proxy.md` and close the ticket, with no merge); `stuck`
  (the failure flow in
  `.agents/skills/launch-task/references/worker-failure.md`, which stops the
  worker).
- The committed origin's worker may produce no new commits of its own if
  verification is clean. That is expected -- the substantive change is already
  on the branch from the live commit.

## Step 4: Merge and go live

A pass the careful flow dispatched for a critical app does not take this step
(`references/critical-app-handoff.md`). Otherwise, on `done`:

1. Run the checks before a merge, in
   `.agents/shared/references/harden-contention.md` ("Before merge: lease,
   freshness, conflicts"): wait out any foreground editing lease on an app or
   service in the pass, confirm the branch is still fresh, and never resolve a
   conflicted merge yourself. A stale or conflicted pass goes back to its
   worker to catch up to your `HEAD`, and its next `done` comes back through
   these same checks.
2. Merge `mngr/update-$SLUG` and destroy the worker (`create_worker.py destroy
   --name update-$SLUG`), as `lead-proxy.md` says for `done` ("Terminal status:
   act and stop polling").
3. Go live for each creation in the pass by its type: `harden-lead.md`,
   "Merge, then go live by type".
4. Close the ticket:

   ```bash
   tk close "$TICKET_ID" "Updated $CREATIONS -- worker branch merged."
   ```
