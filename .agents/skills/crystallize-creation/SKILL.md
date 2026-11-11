---
name: crystallize-creation
description: "Promote the just-finished work into a new, reusable, committed, tested skill. Use when that process would recur with new inputs -- especially something figured out through research or debugging -- or when the user says 'crystallize this'."
metadata:
  author: imbue
---

# Crystallizing a creation into existence

This is the **create** lead of the creation lifecycle. The user has signed off
on a shape -- a process from the just-finished turn, a confirmed data sample,
an app confirmed live -- and you put in the thorough, expensive pass that turns
it into a hardened, committed, reviewed creation, in the background. You
dispatch the build to a harden worker; your role is to package context, launch,
proxy gates, merge, and go live.

Standing instructions:

- **Never crystallize without explicit user go-ahead**: a yes to Step 1, the
  user's explicit invocation, or a confirmed handoff from `build-app` or
  `fetch-process-show`.
- **Never crystallize a turn whose process would not repeat recognizably on a
  re-run.** Model-judgement steps within a stable process do NOT disqualify it.
- **Never harden inline.** Hardening always runs in the background worker.
  Commit pending changes, never stash them, then dispatch.
- **The worker owns outline and implementation decisions**; do not second-guess
  its structure unless something is clearly wrong.
- **You own the poll** from launch until the worker's terminal status, even
  when another skill invoked you and you moved on to other work.
- **After a compaction**, re-read the shared references below before you handle
  the worker's next report.

Three shared references carry the detail, and each step names the section it
needs:

- `.agents/shared/references/harden-lead.md`: the dispatch steps this skill
  shares with `update-creation` and `heal-creation`.
- `.agents/shared/references/lead-proxy.md`: polling, answering gates, acting
  on the worker's terminal status.
- `.agents/shared/references/harden-contention.md`: the checks before any merge
  from the worker's branch.

## Steps

How you got here decides where you start:

| You were invoked | Creation | Start at |
|---|---|---|
| on your own, after a turn | a **skill** reconstructed from the transcript (the common case) | "When to invoke", then Step 1 |
| by the user ("crystallize this", `/crystallize-creation`) | the same | Step 2 |
| by `build-app`, with `type=app` | an **app** `build-app` already built and the user confirmed live | Step 2 |
| by `fetch-process-show`, with `type=skill` and a `source_artifacts_dir` of staged scripts and `sample.json` | a **skill** from a confirmed data sample | Step 2 |

- When to invoke (skill, standalone): whether to propose crystallizing at all
- Step 1: Confirm (skill, standalone only)
- Names
- Step 2: Open a tracking ticket
- Step 3: Write the task file
- Step 4: Launch the worker
- Step 5: Background-poll for worker reports (end your turn each time the poll
  is armed)
- Step 6: Go live, on `done`

The creation's type drives which **gates** the worker emits (Step 5) and the
**go-live** after the merge (Step 6).

## When to invoke (skill, standalone)

Invoke when (1) the just-finished work was a single cohesive unit, (2) a
re-run with different inputs would follow a recognizably similar process, and
(3) the task is likely to recur. **Default to asking the user**, not deciding
silently; if you can name any plausible skill shape, propose it and let them
decide. Read `references/when-to-crystallize.md` before you decline, or when
you are unsure: it has the re-run test and the reasoning traps.

## Step 1: Confirm (skill, standalone only)

**Skip this when the user explicitly invoked crystallize** (typed
`/crystallize-creation`, said "crystallize this / make a skill out of this" in
the prior turn) **or when `build-app` or `fetch-process-show` invoked this
skill** after the user approved a sample or a working app. In those cases go
straight to Step 2.

Otherwise send a one-line pre-gate question to the user:

> "I just did X and Y. Worth crystallizing into a reusable skill? (yes/no)"

Wait for the reply. If no, stop.

## Names

Pick a short kebab-case slug `$NAME` for the creation (e.g. `migrate-config`).
If the skill that invoked you handed you a slug (and a `source_artifacts_dir`),
reuse it. `harden-lead.md` ("Names and paths") has every path that follows:

- Worker and branch: `crystallize-$NAME` / `mngr/crystallize-$NAME`
- Runtime dir and task file: `data/.tasks/harden/crystallize-$NAME/` and
  `data/.tasks/harden/crystallize-$NAME/task.md`

## Step 2: Open a tracking ticket

The ticket survives until the post-merge migration, so record its ID to disk:

```bash
mkdir -p data/.tasks/harden/crystallize-$NAME
TICKET_ID=$(tk create "crystallize $NAME" -t task \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
echo "$TICKET_ID" > data/.tasks/harden/crystallize-$NAME/ticket_id.txt
```

## Step 3: Write the task file

`harden-lead.md` ("The task file") explains the frontmatter fields and the
rules for the body. For a crystallize pass:

- `creations` is the one creation, as `<type>:<name>`: `skill:$NAME`,
  `app:<app name>`, or `service:<package>`.
- `diff_base` is the commit before the work being hardened began:
  - a skill reconstructed from the transcript: your `HEAD` at dispatch;
  - an app: the parent of the commit that added the app's manifest, `$(git
    rev-parse "$(git log --diff-filter=A --format=%H -1 --
    system/apps/<package>/app.toml)^")`, so the scope file's diff covers the
    scaffold and everything built on it;
  - a service: the parent of the commit that added its
    `system/supervisord.conf.d/<name>.conf`, the same way.
- `source_artifacts_dir` is optional: the directory of staged input files a
  skill that invoked you handed over. Step 4's launch copies it to the worker.

The body *describes* the work and -- for a skill reconstructed from the
transcript -- anchors the worker's search with verbatim quotes (the user's
original ask, key decisions, tool outputs that defined the recipe). Describe
invariants and state constraints; do **not** enumerate subcommands, flow steps,
or argparse surfaces -- those are the worker's decisions.

```bash
{
cat << FRONTMATTER_EOF
---
finish_report_path: data/.tasks/harden/crystallize-$NAME/reports/report.md
scope_file: data/.tasks/harden/crystallize-$NAME/scope.json
diff_base: $(git rev-parse HEAD)
operation: crystallize
creations: skill:$NAME
FRONTMATTER_EOF
# If the skill that invoked you staged input files, add this line inside the frontmatter:
#   source_artifacts_dir: data/.tasks/<calling-skill>/$NAME/
cat << FRONTMATTER_CLOSE
---
FRONTMATTER_CLOSE
cat << 'BODY_EOF'

# Task: crystallize the just-finished work into a reusable skill

## What was done
<2-5 sentences describing the work to crystallize: what the user asked for,
what you did, the recipe that emerged. This should make sense to someone who
has not seen the transcript.>

## Anchors (verbatim quotes)
The worker uses these to locate the relevant turns via `mngr transcript`.
Include the user's original request (verbatim) and 1-3 short quotes marking
distinctive moments (a key decision, a tool output that drove a step).
<paste quotes here, one per bullet, in original wording.>

## Preconditions and postconditions
<what must be true about the creation's inputs before it runs, and its outputs
after. Describe the contract; do not prescribe subcommands or surfaces.>

## What to do
Invoke the `harden-worker` skill. On a harness that cannot invoke skills, read
and follow `.agents/skills/harden-worker/SKILL.md` from your own checkout. It
reads `operation` and `creations` from this frontmatter and follows the
matching references. When you reach a gate or terminal status, deliver a report to the lead
per its reporting protocol; the destination is `finish_report_path`.
Whether to split this pass across workers of your own is your own call, per the
harden contract's "Splitting the pass across workers of your own".

## Milestones
Do not save everything for the end. As soon as the creation is genuinely usable
on your branch -- for an app, once its tests pass; for a skill, once your
scenarios pass -- commit and declare a milestone (non-blocking, per your
reporting protocol): the lead merges the first usable version as soon as you
declare it, so the user can start using the creation while you finish the
review gates. Name it for what is true at that commit, and have its `## Tested`
section say exactly what you ran there and what you have not.

## Success criteria
- The creation is committed on your branch, tested, and passes the review gates.
- For a reconstructed skill: the user approved the outline (Gate 1) and the
  final creation (Gate 2), each via a delivered report.
BODY_EOF
} > data/.tasks/harden/crystallize-$NAME/task.md
```

Set `creations: app:<app name>` and the app's `diff_base` (and adjust the body
to point at the already-built lib) when invoked by `build-app`. Fill in the
real `## What was done` and `## Anchors` -- do not leave the placeholders.

## Step 4: Launch the worker

Commit any pending changes first (the just-finished change is exactly what
belongs on the branch); a dirty tree, even from unrelated changes, is never a
reason to do the hardening inline (`harden-lead.md`, "Launching the worker").

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py launch \
    --name crystallize-$NAME \
    --template worker \
    --runtime-dir data/.tasks/harden/crystallize-$NAME/ \
    --task-file data/.tasks/harden/crystallize-$NAME/task.md
```

If the frontmatter sets `source_artifacts_dir`, `launch` copies it to the
worker too -- no extra flag.

## Step 5: Background-poll for worker reports

Arm the poll, which returns at once, then end your turn or continue with other
work (later steps of the skill that invoked you, interface design, other user
requests). Each report arrives in your chat as a message that starts your next
turn; handle it then.

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the background agent" --waiter -- \
    uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py await \
    --name crystallize-$NAME \
    --task-file data/.tasks/harden/crystallize-$NAME/task.md \
    --timeout 90m
```

Without this poll, gate reports never reach the user and the worker deadlocks.
Handle each report as `.agents/shared/references/lead-proxy.md` says (gate
decisions, the "do not interrupt more recent user work" rule, terminal
statuses), and re-arm this poll after every gate and every milestone. For a
crystallize pass:

- Gates: **skill** → `outline-approval` (Gate 1) and `final-creation` (Gate 2);
  **app** → none, since the user confirmed the live app already (the worker
  goes straight to `done`). A `question` gate can arrive at any point.
- Milestones: any name, non-blocking → provisional merge per
  `.agents/shared/references/lead-proxy-milestones.md`, after the checks before
  a merge in `.agents/shared/references/harden-contention.md` ("Before merge:
  lease, freshness, conflicts"). Provisional go-live is the minimum that makes
  the creation usable: **skill** → it is on disk at `.agents/skills/$NAME/` and
  invocable; **app** → refresh the window. Step 6 still runs only on `done`.
- Terminal statuses: `done` (Step 6); `stuck` (the failure flow in
  `.agents/skills/launch-task/references/worker-failure.md`, which stops the
  worker).

## Step 6: Go live

A provisional milestone merge does not change this step: it runs only on `done`,
and the `done` merge brings the remainder of the branch.

On `done`, run the checks before a merge (`harden-contention.md`, "Before
merge: lease, freshness, conflicts"), then merge `mngr/crystallize-$NAME` and
destroy the worker (`create_worker.py destroy --name crystallize-$NAME`; any
workers it split its pass across go with it), as `lead-proxy.md` says for
`done` ("Terminal status: act and stop polling"). Then go live by the
creation's type, as `harden-lead.md` ("Merge, then go live by type") says, with
what a new creation adds:

- **skill**: after the `uv sync --all-packages --frozen` a skill with a
  `python/` project needs, read and follow
  `references/post-crystallize-migration.md` before declaring crystallize
  done -- point consumers at the installed skill path, delete the stale runtime
  dir, pick up any breaking renames the worker introduced, restart any caching
  service, and close the ticket recorded in
  `data/.tasks/harden/crystallize-$NAME/ticket_id.txt`. Commit consumer changes
  as a separate commit.
- **app**: refresh its window so the user sees the merged build, then close
  the ticket.
- **service**: restart its programs, then close the ticket.
