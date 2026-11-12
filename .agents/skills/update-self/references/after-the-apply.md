# After the apply: the report-driven remainder (SKILL.md Steps 5c and 5d)

Read this after an apply that exited 0, before teardown.

## Anything of the user's that is broken gets fixed, not reported

This holds for every update. When something of theirs does not work on the
landed tree (a user program not RUNNING after the restart, per `supervisorctl
status`; a user app that fails its health route; a user skill that no longer
imports; a consumer the report names that the update broke):

1. Say what you are fixing in one short chat line ("One of your apps didn't
   come back after the update -- fixing it now."). Nothing else changes in
   the app or in `run.json`: the pass is still in progress.
2. Dispatch a repair worker with the evidence (the program's status and the
   tail of its stderr log, the failing check's output, what the update
   changed that it touches) through the `launch-task` skill, and proxy its
   gates as `question-gates.md` does. Name `python-packaging-migration.md` to
   it when the failure is one of that file's symptoms.
3. Finish the pass (the rest of this file, then teardown) once it is fixed,
   or once the repair worker gives up. A give-up is still recorded as
   `UPDATED`, with a `--detail` line naming what is still broken and what was
   tried, and the results message says the same in plain words.

## What each finding of the report asks of you

The worker classified provisioning findings by
`worker-provisioning-changes.md` (live-applicable or rebuild-only); read it
when a finding's class or wording is unclear.

- **`shared_runtime` live consumers.** The apply's restart has already put
  every built-in service on the merged code. What the report can still name
  is a *user-created* consumer (an app or skill of theirs reading something
  the update changed): check it, and fix any breakage as above.
- **`Dockerfile`.** Apply the live-applicable hunks the report calls out.
  Version pins live in `setup_system.sh`, already re-run; keep
  `agent_types.claude.version` in `.mngr/settings.toml` in sync. An
  image-level hunk the report marks *pending recreate* is a results-message
  caveat naming what the workspace lacks until a recreate. One marked
  *covered live* changes nothing a recreate would fix, so leave it out.
- **Rebuild-only flags.** Surface each as needing a workspace recreate,
  saying what the running workspace lacks until then. Never imply they are
  live.
- **A user-created dependent of a global-dependency bump.** The worker could
  not verify it in its worktree: check it now, and fix any breakage as above.
- **An `extra_provision_command` the update added.** Run that command live.
- **A user app whose `app.toml` has no `stop_when_no_windows` field.** Decide
  it per `older-workspaces.md` ("Apps built before `stop_when_no_windows`").

## Escalating built-in defects

Before composing the results message, collect every finding the worker
labelled a `submit-upstream-changes` candidate, plus any other defect in
built-in code you hit this pass: a failing built-in test, a built-in test the
worker's Validation names as flaky, a step of this skill that broke and had
to be worked around. Escalate them together as AGENTS.md's "Updates" section
describes: one report through
`data/.tasks/update-self/skill-at-target/.agents/shared/references/report-built-in-issues.md`,
or `submit-upstream-changes` for a template fix. Otherwise name each in the
results message with the submission offered.

A flaky test of a creation the workspace built is not built-in: file a
regular ticket for it (`tk create`).

Then compose the results message per `results-message.md`.
