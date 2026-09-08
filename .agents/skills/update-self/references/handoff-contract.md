# The cross-version handoff contract

Maintainer notes for editing the update-self skill. The pass runs across two
versions of this skill at once: Steps 1-2 from the workspace's *local* copy,
and Step 3 onward from the copy staged at
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self` -- the
target release's own copy (or the local one, when the ref predates the skill).

## What must stay stable

- **Steps 1-2 always run from the local copy.** They are what decide `$REF`,
  so by construction they cannot come from the target. A future version's
  Steps 1-2 must stay "capture a backup, the lease and clean-tree checks, then
  resolve a ref into `$REF`".
- **The target's flow is entered at Step 3**, and everything from there on --
  the worker dispatch, the report audit, and the apply (Step 5b runs the
  staged copy's `update_self.py apply`) -- is the target version's. A future
  version's Step 3 must stay the worker dispatch; otherwise an older initiator
  handing off into a newer copy (or vice versa) lands at the wrong step.
- **Step 3a's ceiling re-check** must stay: it is the only ceiling check that
  runs on a workspace arriving from a template that predates the ceiling (see
  `version-ceiling.md`).
- **The staging path** must stay
  `data/.tasks/update-self/skill-at-target/.agents/skills/update-self`, for the
  same reason.
- **Every `update_self.py` invocation from Step 3 on uses the staged copy** (a
  test holds the prose to this). A relative
  `.agents/skills/update-self/scripts/update_self.py` resolves to the
  workspace's own copy, which may predate a subcommand the prose relies on --
  the `run-status` writes in particular fail exactly on the first update into
  the release that ships them.
- **The task-file template keeps `lead_agent` and `finish_report_path`** (a
  test holds this too): an older workspace's lead follows the staged prose but
  launches with its own `launch-task/create_worker.py`, which may predate
  launch-time `lead_agent` stamping; under an old launcher the line is the
  only thing that gives the worker a report address.
- **The prose asks of `create_worker.py` only what every supported launcher
  provides** (`scripts/launcher_contract_test.py` pins that set to the oldest
  release the app updates from). Clearing the previous pass's worker uses
  plain `mngr list` / `mngr destroy` for this reason.

## What the apply must tolerate

Because the apply runs from the staged copy, an old workspace updating in runs
the *target's* apply against its own pre-merge tree. Fixes to the apply flow
take effect for the very update that ships them, which also means the apply
must keep tolerating older pre-merge trees: guarded imports (the
`oom_priority` bands module is loaded off the pre-merge tree and refused
wholesale when it lacks any attribute the apply reads), no assumptions about
the pre-merge layout, and a `scripts/` directory that is staged and run as one
unit (the entry script imports its sibling modules by name from its own
directory).

## Trust

The handoff runs the target ref's `update_self.py` and follows its prose
*before* the worker has validated anything. For the default target (a stable,
already-tested `minds-v*` tag) that is the same trust basis as the merge
itself; a `--override` to an untrusted ref means trusting that ref's flow code
and instructions. Only override to a ref you trust.
