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

`<TASK_FILE>` is the `task_file` value in the frontmatter you were sent --
the launcher stamps this task file's own path there before sending it, so you
always hold an exact path. `TASK_FILE` is that path; `LEAD_AGENT` is the `mngr`
agent you push reports to (and whose transcript you read); `FINISH_REPORT_PATH`
is the destination path on the lead's worktree where your report file must land
-- the lead polls for exactly this file. Any additional string fields the lead
set in the frontmatter also become shell variables -- see your worker
SKILL.md for which extras (if any) the calling flow stages.

`LEAD_AGENT` may legitimately be unset: a launcher that predates
launch-time stamping does not write it (the parser warns instead of
failing). That never blocks reporting -- the `report` subcommand below
falls back to resolving your lead from your own agent's label.

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
   allows (below). The subcommand reads `finish_report_path` and `lead_agent`
   from your task file, writes `report.md` beside `finish_report_path` in your
   own tree, and pushes that directory to the lead so it lands at the lead's
   `FINISH_REPORT_PATH`. If that push fails, or `lead_agent` was never stamped,
   it resolves your own `lead_agent` label and copies the report into that
   lead's work dir instead.

   If it can deliver the report nowhere it exits 2. That is a hard failure, not
   something to route around: never end a run with the report sitting only in
   your own worktree -- a finished worker that cannot say so looks identical to
   a hung one from the lead's side.

3. Stop your turn. For gate reports, the lead sends the user's reply via
   `mngr message` and you resume; for terminal reports, the lead acts on the
   report and the run ends.

The push is the ready signal -- it only happens once you are finished writing.
Do not report a partial.

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
