---
name: harden-worker
description: Run the background harden pass for one artifact -- crystallize, update, or heal a skill, a service, or the system interface in an isolated worktree, then report back. Invoke when your task file hands you an artifact to harden; it names the operation and artifact to compose.
metadata:
  role: worker-sub-skill
---

# Hardening an artifact (generic worker)

You are the single worker that runs every background harden pass. Your task file
names one **operation** and one **artifact**, and your whole job is to compose
three references and follow them. You own nothing operation- or
artifact-specific yourself.

## Step 1: Read your task file and resolve inputs

Your task file was synced to your worktree under `runtime/harden/<slug>/task.md`.
Extract the lead address and the report destination (plus the `operation` and
`artifact` fields the lead set in frontmatter):

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py 'runtime/harden/*/task.md')"
```

This sets `LEAD_AGENT`, `FINISH_REPORT_PATH`, `OPERATION`, and `ARTIFACT`. Fail
loudly if `OPERATION` or `ARTIFACT` is unset -- the lead must supply both.

- `OPERATION` is one of `crystallize`, `update`, `heal`.
- `ARTIFACT` is one of `skill`, `service`, `system-interface`.

## Step 2: Load the three references that define your run

Read all three, in this order:

1. `.agents/shared/references/harden-artifact.md` -- the universal contract (the
   bar, isolation, reporting, testing/hardening, review gates,
   preserve-and-surface, give-up). Always.
2. `.agents/shared/references/op-<OPERATION>.md` -- your operation's pre-work,
   stages, and the exact gate / terminal-status `name:` values that apply.
3. `.agents/shared/references/artifact-<ARTIFACT>.md` -- where the artifact
   lives, how to run/test it in isolation, scenario specifics, the final-gate
   body template, and the don't-touch list.

Then follow them. Where the operation reference and the artifact reference both
speak to the same stage (e.g. the final gate), the operation reference owns the
*shape* (which gates fire, in what order) and the artifact reference owns the
*content* (the body template, the layout, the test mechanics).

## Step 3: Report back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure. The `eval` in Step 1 already set the variables it needs. Substitute:

- `<TASK_FILE_GLOB>` -> `runtime/harden/*/task.md`
- `<RUNTIME_REPORTS_DIR>` -> the directory part of `FINISH_REPORT_PATH`
  (i.e. `dirname "$FINISH_REPORT_PATH"`).

The valid `name:` values for gates and terminal statuses come from your
operation reference -- it is the authority on which gates fire for your
operation × artifact combination (e.g. a crystallized service emits no gates; a
crystallized skill emits `outline-approval` then `final-artifact`).

That is the entire worker. Everything else is in the three references.
