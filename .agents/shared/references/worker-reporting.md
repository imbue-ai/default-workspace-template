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

Quote the pattern. `LEAD_AGENT` is the `mngr` agent you push reports to
(and whose transcript you read); `FINISH_REPORT_PATH` is the destination
path on the lead's worktree where your report file must land -- the lead
polls for exactly this file. Any additional string fields the lead
set in the frontmatter also become shell variables -- see your worker
SKILL.md for which extras (if any) the calling flow stages.

`LEAD_AGENT` may legitimately be unset: a launcher that predates
launch-time stamping does not write it (the parser warns instead of
failing). That never blocks reporting -- use the fallback delivery in
step 2.

## Reporting procedure

At each gate or terminal status:

1. Write your report to `<RUNTIME_REPORTS_DIR>/report.md` (create the directory
   if missing). `report.md` is the basename of `FINISH_REPORT_PATH`, so pushing
   the directory in step 2 lands it at the lead's `FINISH_REPORT_PATH`.

   ```
   ---
   type: gate | status
   name: <skill-specific marker>
   ---

   <body: the message the user needs to see, addressing the user directly>
   ```

2. Sync the report directory to the lead:

   ```bash
   mngr rsync ./<RUNTIME_REPORTS_DIR>/ \
       "$LEAD_AGENT:$(dirname "$FINISH_REPORT_PATH")/" \
       --uncommitted-changes=merge
   ```

   `mngr rsync` takes `SOURCE DESTINATION`: your local `<RUNTIME_REPORTS_DIR>/`
   first, then the lead endpoint. `LEAD_AGENT` / `FINISH_REPORT_PATH` come from
   the `eval` above; `<RUNTIME_REPORTS_DIR>` is your worker SKILL.md's local
   reports dir. mngr treats an argument as a local path only when it starts with
   `/`, `./`, `../`, or `~/` (hence the `./` on the source; a bare `data/foo`
   reads as an agent name), and a relative path on the lead endpoint resolves
   against the lead's workdir. You sync the report's *parent directory*
   (`dirname`) rather than the file itself: the trailing slashes matter (rsync
   directory semantics) and rsync cannot transfer a single file.
   `--uncommitted-changes=merge` is required because the lead's worktree usually
   has uncommitted local state.

   **Fallback delivery (same-repo)**: when `LEAD_AGENT` is unset/empty, or the
   `mngr rsync` push fails, deliver the report by writing it straight into the
   lead's workspace. Your worktree hangs off the lead's own git repo, so the
   repo's *main* worktree is the lead's workspace and the lead polls the same
   `FINISH_REPORT_PATH` relative to it:

   ```bash
   LEAD_WORKTREE="$(git worktree list --porcelain | head -1 | sed 's/^worktree //')"
   mkdir -p "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")"
   cp "<RUNTIME_REPORTS_DIR>/report.md" "$LEAD_WORKTREE/$FINISH_REPORT_PATH"
   ```

   Never end a run with the report sitting only in your own worktree -- a
   finished worker that cannot say so looks identical to a hung one from the
   lead's side.

3. Stop your turn. For gate reports, the lead sends the user's reply via
   `mngr message` and you resume; for terminal reports, the lead acts on the
   report and the run ends. Only gates and terminal statuses stop your turn:
   the milestone reports below are non-blocking, and you keep working straight
   through one.

The sync is the ready signal -- it only happens once you are finished writing.
Do not sync a partial report.

## Milestone reports (non-blocking)

A **milestone** says that one specific commit on your branch is already worth
using, well before the pass finishes. It is not a gate: nothing is asked of the
lead, nothing is waited for, and you do not stop your turn. The lead may merge
that exact commit and let the user start using the creation while you carry on
hardening.

Declare one whenever you reach a commit the lead could start using before
`done` -- typically the first commit at which the creation runs end to end, and
again at any later commit that is a real step up in what works. Your operation
reference (or, for a plain task, the task file) may say *when* its flow expects
a milestone; the name is always yours to pick.

1. **Commit first.** The lead merges the exact commit you name, so make the
   commit before you write the file, and leave the tree clean.

2. **Write the milestone file** at
   `<RUNTIME_REPORTS_DIR>/milestones/<sha7>-<name>.md` (create the directory if
   missing). It sits beside `report.md`, one file per milestone, so it never
   competes for the single `report.md` slot:

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

   `<name>` is a kebab-case slug (`[a-z0-9]+(-[a-z0-9]+)*`) that you choose to
   describe what is true at that commit. There is no fixed list of milestone
   names and nothing validates them -- pick the words that fit this task.
   Because the commit's short sha is in the filename, declaring the same name
   again at a later commit is a new file, and so a new event for the lead.

   The `## Tested` section is **required**, and it is written for the lead: it
   says how much trust this build deserves, and it lets the lead skip
   re-running anything you name as passing at this exact commit (anything you
   do not name, the lead may run itself). Name the commands, suites, scenarios,
   and review gates you actually ran and their result, and say plainly what you
   have not run yet.

3. **Sync the reports directory to the lead**, exactly as in step 2 of the
   reporting procedure above -- same `mngr rsync`, same same-repo fallback:

   ```bash
   mngr rsync ./<RUNTIME_REPORTS_DIR>/ \
       "$LEAD_AGENT:$(dirname "$FINISH_REPORT_PATH")/" \
       --uncommitted-changes=merge
   ```

   The push carries the whole directory, so the new `milestones/` file rides
   along with it and the lead's own `consumed/` is left untouched. When
   `LEAD_AGENT` is unset/empty or the push fails, use the same-repo fallback,
   copying into the lead's `milestones/` directory:

   ```bash
   LEAD_WORKTREE="$(git worktree list --porcelain | head -1 | sed 's/^worktree //')"
   mkdir -p "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")/milestones"
   cp "$MILESTONE_FILE" "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")/milestones/"
   ```

4. **Continue working.** Do not stop your turn, and do not wait for the lead to
   acknowledge or merge. Delivery is best-effort: if both the push and the
   fallback fail, keep going -- your `done` report still hands over the whole
   branch.

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
