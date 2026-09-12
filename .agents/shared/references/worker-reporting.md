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
agent has no work dir to stamp (the parser warns instead of failing). That never
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
   and the run ends.

The delivery is the ready signal -- it only happens once you are finished
writing. Do not deliver a partial report.

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
