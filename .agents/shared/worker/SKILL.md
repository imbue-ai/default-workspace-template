---
name: harden-worker
description: Run the background harden pass for a change -- crystallize, update, or heal the skills, apps, and services it covers in an isolated worktree, then report back. Invoke when your task file hands you creations to harden; it names the operation and each creation's type to compose.
metadata:
  author: imbue
  role: worker-sub-skill
---

# Hardening a creation (generic worker)

You are the single worker that runs every background harden pass. Your task file
names one **operation** and the **creations** the pass covers -- one, or several
when the change spans them -- each with its **type**, and your whole job is to
load the references they select and follow them. A pass over several creations
is one pass: you harden the change as a whole, against one branch and one test
gate. You own no operation- or
type-specific *behavior* yourself -- that all lives in the references; Step 2
just routes you to the right ones.

## Step 1: Read your task file and resolve inputs

Your task file was synced to your worktree, and its frontmatter names its own
path in `task_file`. Pass that exact path to the parser to extract the lead
address, the report destination, the `operation`, and the `creations` the lead
set in frontmatter:

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py <TASK_FILE>)"
```

This sets `TASK_FILE`, `LEAD_AGENT`, `LEAD_WORK_DIR`, `FINISH_REPORT_PATH`,
`OPERATION`, `CREATIONS`, `SCOPE_FILE`, and `DIFF_BASE`. The parser refuses a
task that names an operation without `creations` and `scope_file`; fail loudly
if `OPERATION` or `DIFF_BASE` is unset. `DIFF_BASE` is the commit before the
work being hardened: the pass's footprint diff is taken from it, and the test
gate selects its suites from the part of that diff that is the pass's
(`harden-creation.md` has both commands). `SCOPE_FILE` is where you write that footprint.

- `OPERATION` is one of `crystallize`, `update`, `heal`.
- `CREATIONS` lists every creation the pass covers as `<type>:<name>`,
  space-separated (`for creation in $CREATIONS; do echo "${creation%%:*}
  ${creation#*:}"; done`). The type is `skill`, `app`, `service`, or `shared`:
  a shared script or reference, named by its path under `.agents/shared/`,
  which you harden as a skill (`type-skill.md`).

The references below speak of "the creation" and "your type"; in a pass over
several creations, read them as applying to each creation in turn, and load a
type's reference once however many creations share it.

## Step 2: Load the references that define your run

Read these top to bottom, then follow them.

Filenames are relative to `.agents/shared/worker/references/`, except the two
marked **[shared]**, which live in `.agents/shared/references/`.

| Load when | Reference | What it gives you |
|---|---|---|
| every run | `harden-creation.md` | the universal contract: the bar, isolation, reporting, the scope file, testing/hardening, review gates, preserve-and-surface, give-up |
| every run | `op-<OPERATION>.md` | your operation's spine: pre-work, stages, which gates fire and their `name:` values, gate report templates |
| every run, once per type among your creations | `type-<TYPE>.md` (`type-skill.md` for a `shared` creation) | the creation itself: where it lives, how to run/test it in isolation, how to edit it safely |
| every run | `worker-reporting.md` **[shared]** | the report-file procedure and task-file frontmatter schema (Step 3 uses it) |
| you reconstruct the work from the lead's session | `transcript-exploration.md` | how to find the work in the lead's transcript |
| a creation is a `skill` | `spec-summary.md` **[shared]** | the agentskills.io layout/spec authority |
| a creation is a `skill`, and your operation runs an outline gate | `skill-outline-fields.md` | what goes inside the outline gate |
| a creation is a `skill`, on an emergent `update` | `update-vs-create-new.md` | update-in-place vs. split-a-new-sibling |
| a creation is an `app` | `web-frontend-testing.md` | isolated-instance and rendered-page rules |

The two conditional cases that need defining:

- **"you reconstruct the work from the lead's session"** -- a skill
  `crystallize`, an emergent `update`, or any `heal`: the work or incident you
  must reproduce lives only in the conversation. The other runs are handed a
  materialized source instead and skip `transcript-exploration.md` -- a committed
  `update` reads the diff, and an app `crystallize` reads the pre-built app on
  disk.
- **"runs an outline gate"** -- a skill `crystallize` or an emergent skill
  `update`. A committed `update` and any `heal` have no outline gate, so they
  never use `skill-outline-fields.md` or `update-vs-create-new.md` (loading them
  anyway is harmless).

The operation reference is the lifecycle spine -- it owns the stages, which gates
fire and in what order, and the report templates. The type reference is the
operation-agnostic description of the thing you are hardening. Where the
operation reference needs a type-specific value (a gate template's field list,
the crystallize shape), it carries that itself, keyed by type.

## Step 3: Report back to the lead

Report with the launcher's `report` subcommand -- write the body to a file and
hand that file over -- exactly as
`.agents/shared/references/worker-reporting.md` describes. The `eval` in Step 1
already set the variables it needs, and the subcommand derives the report
destination and the lead's address from `TASK_FILE` itself, so there is nothing
to substitute.

The valid `name:` values for gates and terminal statuses come from your
operation reference -- it is the authority on which gates fire for your
operation × type combination (e.g. a crystallized app emits no gates; a
crystallized skill emits `outline-approval` then `final-creation`). A pass over
several creations emits each gate once, covering all of them. On top of
whatever it lists, `question` (`type: gate`) is valid on every run: any worker
may stop mid-flight and ask its lead, whatever the operation.

One thing a worker never does: request a secret. There is no chat here to show a
secret card in, so a credential the work turns out to need (an API key for a
service, a token for an integration) goes into your report as a need the lead
requests through the `connect-external-service` skill, with the file and variable
names you would have asked for.

That is the entire worker. Everything else is in the references you loaded in
Step 2.
