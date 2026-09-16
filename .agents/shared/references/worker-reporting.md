# Worker reporting contract

Generic file-based protocol for signaling the lead at each gate and at
terminal status. The worker supplies flow-specific runtime paths and the
enum of allowed `name:` values.

## Task-file inputs

Your task file has been synced to your worktree at `<RUNTIME_DIR>/task.md`.
Your worker SKILL.md lists any additional inputs the calling flow stages
alongside it. At the start of your run, extract the lead's address with:

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py <TASK_FILE>)"
```

`<TASK_FILE>` is the `task_file` value in the frontmatter you were sent -- the
launcher stamps this task file's own path there before sending it, so you always
hold an exact path, and `TASK_FILE` is that path. `LEAD_AGENT` is the `mngr`
agent id of the agent that dispatched you (an `agent-<hex>` value; older
launchers stamped its name, which a rename of the lead's chat invalidates
mid-task, so never resolve or copy it as a name). It is the agent whose
transcript you read (`mngr transcript $LEAD_AGENT`): mngr knows agents, not
chats, so it names the agent even though the lead's chat may have run on others
before it. `LEAD_WORK_DIR` is the lead's own checkout, where your report must
land, and `FINISH_REPORT_PATH` is the report's path relative to it -- the lead
polls for exactly this file. Any additional string fields the lead set in the
frontmatter also become shell variables -- see your worker SKILL.md for which
extras (if any) the calling flow stages.

`LEAD_AGENT`, `LEAD_WORK_DIR` and `TASK_FILE` may legitimately be unset: a
launcher that predates launch-time stamping does not write them, and a launch
from outside an agent has no work dir to stamp (the parser warns about a missing
`LEAD_AGENT` and passes the others through only when the frontmatter has them).
That never blocks reporting -- the `report` subcommand below falls back to the
lead's work dir as `mngr list` reports it.

## Reporting procedure

At each gate or terminal status:

1. Write the report **body** to a file in your worktree (the body only, no
   frontmatter): the message the user needs to see, addressing the user
   directly.

2. Hand that file to the launcher, which builds the report, delivers it, and
   prints where it landed:

   ```bash
   uv run .agents/skills/launch-task/scripts/create_worker.py report \
       --task-file "$TASK_FILE" \
       --type gate \
       --name question \
       --body-file <BODY_FILE>
   ```

   `--type` is `gate` or `status` and `--name` is one of the values your flow
   allows (below). The subcommand reads `finish_report_path` and `lead_work_dir`
   from your task file, writes `report.md` beside `finish_report_path` in your
   own tree, and copies it to the same relative path under the lead's checkout,
   which is where the lead polls. Your worktree hangs off the lead's own git
   repo on the same host, so that is a plain local copy at any depth of
   dispatch. If `lead_work_dir` was never stamped, it resolves the lead's work
   dir from `mngr list` instead.

   If it can deliver the report nowhere it exits 2. That is a hard failure, not
   something to route around: never end a run with the report sitting only in
   your own worktree -- a finished worker that cannot say so looks identical to
   a hung one from the lead's side.

3. Stop your turn. For gate reports, the lead's reply arrives as a message in
   your chat and you resume; for terminal reports, the lead acts on the report
   and the run ends. Only gates and terminal statuses stop your turn: the
   milestone reports below are non-blocking, and you keep working straight
   through one.

The delivery is the ready signal -- it only happens once you are finished
writing. Do not report a partial.

## Report shape

What the subcommand writes, and what the lead parses:

```
---
type: gate | status
name: <skill-specific marker>
---

<body: the message the user needs to see, addressing the user directly>
```

`type: gate` means you are stopping for an answer; `type: status` is terminal.

## `name: question` is valid on every run

Whatever `name:` values your worker SKILL.md and operation reference list,
`question` (`type: gate`) is always available in addition to them: any worker
may stop mid-flight and ask its lead, on every operation. Use it when the
answer is not in your task file or the repo. Your lead answers what it can
itself; a lead that is itself a worker re-raises the question to its own lead
and forwards the answer back down.

## Milestone reports (non-blocking)

A **milestone** names one commit on your branch that is already worth using
before the pass finishes. It is not a gate: the lead may merge that exact
commit while you carry on. Declare one at the first commit where the creation
runs end to end, and again at any later commit that is a real step up. Your
operation reference (or, for a plain task, the task file) may say *when*; the
name is always yours to pick.

1. **Commit first.** The lead merges the exact commit you name; leave the tree
   clean.

2. **Write the milestone file** at
   `<RUNTIME_REPORTS_DIR>/milestones/<sha7>-<name>.md`, where
   `<RUNTIME_REPORTS_DIR>` is `$(dirname "$FINISH_REPORT_PATH")` (create the
   directory if missing) -- one file per milestone, beside `report.md`, never
   in its slot:

   ```bash
   mkdir -p <RUNTIME_REPORTS_DIR>/milestones
   COMMIT="$(git rev-parse HEAD)"          # the full sha for the frontmatter
   MILESTONE_FILE="<RUNTIME_REPORTS_DIR>/milestones/${COMMIT:0:7}-<name>.md"
   ```

   ```
   ---
   type: milestone
   name: <free-form slug chosen by the worker>
   commit: <full sha on the worker's branch>
   branch: mngr/<worker-name>
   ---

   <what is usable now and how to use it, addressed to the user>

   ## Tested
   <what has been verified at this commit: the exact test commands or suites
   run and their result, scenarios exercised, review gates passed -- and,
   explicitly, what has NOT been run yet>

   ## Still pending
   <what the worker will do next before `done`>
   ```

   `<name>` is a kebab-case slug (`[a-z0-9]+(-[a-z0-9]+)*`) you choose to
   describe what is true at that commit; there is no fixed list. The short sha
   in the filename makes the same name at a later commit a new event.

   `## Tested` is **required** and is for the lead: it says how much trust the
   build deserves, and the lead skips re-running anything you name as passing
   at this exact commit. Name what you actually ran and its result, and say
   plainly what you have not run yet.

3. **Copy the milestone into the lead's checkout** -- the same delivery the
   `report` subcommand makes, spelled out because a milestone has no subcommand
   yet:

   ```bash
   LEAD_WORKTREE="${LEAD_WORK_DIR:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}"
   mkdir -p "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")/milestones"
   cp "$MILESTONE_FILE" "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")/milestones/"
   ```

   `LEAD_WORK_DIR` is the lead's own checkout; without it, the repo's main
   worktree is the lead's work dir for every chat agent. Delivery is a plain
   copy because your worktree hangs off the lead's own repo on the same host.

4. **Continue working.** Do not stop your turn or wait for the lead. Delivery
   is best-effort: if both the push and the fallback fail, keep going -- `done`
   still hands over the whole branch. Every later push re-delivers the file
   (you keep your copy); the lead's `await` recognises one it has already
   returned by its name, so that is harmless.

## Terminal status report bodies

Each worker's SKILL.md lists which of these terminal statuses apply. The body
shapes are shared:

### `name: done`

```
Committed on branch `<branch-name>`. Ready to merge.
```

For verify-only flows (no new worker commits), substitute "Verified on branch
`<branch-name>`. Ready to merge." and optionally add: "No follow-up commits
needed; the substantive change is already on the branch from the live commit."

### `name: stuck`

A one-sentence reason and, if applicable, a recommendation for next steps:

```
I could not <do-the-task> because: <reason>. <optional: where work is, recommended next step>.
```

The flow-specific guidance for *when* to give up lives in each worker's
SKILL.md.

### `name: no-update-needed`

```
No update needed. Reason: <one-sentence>.
```

Do not commit a null change.
