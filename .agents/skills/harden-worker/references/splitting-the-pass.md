# Splitting the pass across workers of your own

A conditional part of the harden contract (`harden-creation.md`, beside this
file; its section of the same name has the rule for when to split). Read it
once you have decided to split.

Launch each worker with the launch-task skill exactly as a chat agent would
(`.agents/skills/launch-task/SKILL.md`). Name each one with your own worker
name as the prefix (`update-roadmap-ingest`, `update-roadmap-frontend`): names
are unique across the host in every state, so a re-run of the same pass reaches
the same names only after the old pass has been superseded and destroyed. You
are its lead, so you follow the lead-proxy flow
(`.agents/shared/references/lead-proxy.md`) for it, and the rules that are
yours alone are in
`.agents/shared/references/lead-proxy-intermediate-lead.md`: answer your
worker's `question` yourself or re-raise it to your own lead, merge exactly one
level with `--no-ff`, destroy a merged worker and stop a stuck one, and await
it with `--timeout 60m`.

- Each worker's task file carries the same `operation` as yours, the
  `creations` its boundary touches, its own `scope_file` beside its task file
  (Step 1 of the harden-worker skill fails loudly without them), its boundary
  in prose, and your `diff_base`. Say in the body that it runs only its
  scope's tests and **skips the "Review gates" section of the harden
  contract**, because you run that verification once on the merged result, and
  that a small out-of-scope edit is allowed but must be listed in its `done`
  report.
- When a worker's `question` decides a shared interface, use your judgement per
  case; the default is to message the affected worker with the decision
  immediately (`mngr message`) rather than let it find out at merge time.
- Merge your workers in a fixed order and resolve any conflicts yourself. Then
  run exactly the verification a direct pass runs -- the "Review gates" section
  of the harden contract, against your own scope file, not one of theirs --
  once on the merged result, and report `done` with the same body a direct pass
  would.
