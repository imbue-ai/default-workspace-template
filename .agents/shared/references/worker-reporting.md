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

Quote the pattern. `LEAD_AGENT` is the lead's `mngr` agent id (an
`agent-<hex>` value; older launchers stamped its name, which a rename of the
lead's chat invalidates mid-task, so never resolve or copy it as a name). It
is the address you rsync to when the lead's work dir is not this repo's main
worktree, and the agent whose transcript you read; `FINISH_REPORT_PATH` is
the destination path on the lead's worktree where your report file must
land -- the lead polls for exactly this file. Any additional string fields
the lead set in the frontmatter also become shell variables -- see your
worker SKILL.md for which extras (if any) the calling flow stages.

`LEAD_AGENT` may legitimately be unset: a launcher that predates
launch-time stamping does not write it (the parser warns instead of
failing). That never blocks reporting -- the primary delivery in step 2
does not need it.

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

2. Deliver the report by writing it straight into the lead's workspace. Your
   worktree hangs off the lead's own git repo, so the repo's *main* worktree is
   the lead's workspace (every chat agent's work dir is the workspace root) and
   the lead polls the same `FINISH_REPORT_PATH` relative to it:

   ```bash
   LEAD_WORKTREE="$(git worktree list --porcelain | head -1 | sed 's/^worktree //')"
   mkdir -p "$LEAD_WORKTREE/$(dirname "$FINISH_REPORT_PATH")"
   cp "<RUNTIME_REPORTS_DIR>/report.md" "$LEAD_WORKTREE/$FINISH_REPORT_PATH"
   ```

   **Lead in a worktree of its own** (its work dir is not the repo's main
   worktree, e.g. a worker that launched a worker): the write above lands in the
   wrong checkout, so push the report directory to the lead by its id instead:

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
   has uncommitted local state. If the push fails, or `LEAD_AGENT` is unset, fall
   back to the write above.

   Never end a run with the report sitting only in your own worktree -- a
   finished worker that cannot say so looks identical to a hung one from the
   lead's side.

3. Stop your turn. For gate reports, the lead's reply arrives as a message in
   your chat and you resume; for terminal reports, the lead acts on the report
   and the run ends.

The sync is the ready signal -- it only happens once you are finished writing.
Do not sync a partial report.

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
