# Worker reporting contract

Generic file-based protocol for signaling the lead at each gate and at
terminal status. The worker supplies flow-specific runtime paths and the
enum of allowed `name:` values.

## Task-file inputs

Your task file has been synced to your worktree at `<RUNTIME_DIR>/task.md`.
Your worker SKILL.md lists any additional inputs the calling flow stages
alongside it. At the start of your run, extract the lead's address with:

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py '<TASK_FILE_GLOB>')"
```

Quote the pattern. `LEAD_AGENT` is the `mngr` agent id of the agent that
dispatched you (an `agent-<hex>` value; older launchers stamped its name,
which a rename of the lead's chat invalidates mid-task, so never resolve or
copy it as a name). It is the agent whose transcript you read (`mngr
transcript $LEAD_AGENT`): mngr knows agents, not chats, so it names the agent
even though the lead's chat may have run on others before it.
`LEAD_WORK_DIR` is the lead's own checkout, where your report must land, and
`FINISH_REPORT_PATH` is the report's path relative to it -- the lead polls for
exactly this file. Any additional string fields the lead set in the frontmatter
also become shell variables -- see your worker SKILL.md for which extras (if
any) the calling flow stages.

`LEAD_AGENT` and `LEAD_WORK_DIR` may legitimately be unset: a launcher that
predates launch-time stamping does not write them, and a launch from outside an
agent has no work dir to stamp (the parser warns about a missing `LEAD_AGENT`
and passes `LEAD_WORK_DIR` through only when the frontmatter has it). That never
blocks reporting -- the delivery in step 2 falls back to the repo's main
worktree, which is the lead's work dir for every chat agent.

## Reporting procedure

At each gate or terminal status:

1. Write your report to `<RUNTIME_REPORTS_DIR>/report.md` (create the directory
   if missing). `report.md` is the basename of `FINISH_REPORT_PATH`, so
   delivering it in step 2 lands it at the lead's `FINISH_REPORT_PATH`.

   ```
   ---
   type: gate | status
   name: <skill-specific marker>
   ---

   <body: the message the user needs to see, addressing the user directly>
   ```

2. Deliver the report by writing it straight into the lead's work dir. Your
   worktree hangs off the lead's own git repo on the same host, so the lead's
   checkout is a plain local path for you: `LEAD_WORK_DIR` when the launcher
   stamped it (a lead in a worktree of its own, such as a worker that launched a
   worker, is reached this way), else the repo's *main* worktree (every chat
   agent's work dir is the workspace root). The lead polls the same
   `FINISH_REPORT_PATH` relative to it:

   ```bash
   LEAD_WORKTREE="${LEAD_WORK_DIR:-$(git worktree list --porcelain | head -1 | sed 's/^worktree //')}"
   mkdir -p "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")"
   cp "<RUNTIME_REPORTS_DIR>/report.md" "$LEAD_WORKTREE/$FINISH_REPORT_PATH"
   ```

   Never end a run with the report sitting only in your own worktree -- a
   finished worker that cannot say so looks identical to a hung one from the
   lead's side.

3. Stop your turn. For gate reports, the lead's reply arrives as a message in
   your chat and you resume; for terminal reports, the lead acts on the report
   and the run ends. Only gates and terminal statuses stop your turn: the
   milestone reports below are non-blocking, and you keep working straight
   through one.

The delivery is the ready signal -- it only happens once you are finished
writing. Do not deliver a partial report.

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
   `<RUNTIME_REPORTS_DIR>/milestones/<sha7>-<name>.md` (create the directory if
   missing) -- one file per milestone, beside `report.md`, never in its slot:

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

3. **Sync the reports directory to the lead**, exactly as in step 2 above:

   ```bash
   mngr rsync ./<RUNTIME_REPORTS_DIR>/ \
       "$LEAD_AGENT:$(dirname "$FINISH_REPORT_PATH")/" \
       --uncommitted-changes=merge
   ```

   When `LEAD_AGENT` is unset/empty or the push fails, use the same-repo
   fallback, copying into the lead's `milestones/` directory:

   ```bash
   LEAD_WORKTREE="$(git worktree list --porcelain | head -1 | sed 's/^worktree //')"
   mkdir -p "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")/milestones"
   cp "$MILESTONE_FILE" "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")/milestones/"
   ```

4. **Continue working.** Do not stop your turn or wait for the lead. Delivery
   is best-effort: if both the push and the fallback fail, keep going -- `done`
   still hands over the whole branch.

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
