# The cross-version handoff contract

Maintainer notes: read this before editing the update-self skill, not while
running a pass. A pass runs across two versions of this skill at once: Steps
1-2 from the workspace's *local* copy, and Step 3 onward from the copy staged
at `data/.tasks/update-self/skill-at-target/.agents/skills/update-self` -- the
target release's own copy (or the local one, when the ref predates the skill).
What holds on every pass because of that is here. What a pass does
differently on an older workspace is in `older-workspaces.md`, and for the
worker in `worker/older-workspaces.md`.

## Contents

- What must stay stable
- What can go as the floor rises
- The scheduled entry: a pinned target and ceiling
- What the apply must tolerate
- What the what's-new listing assumes of each release
- Trust

## What must stay stable

- **Steps 1-2 always run from the local copy.** They are what decide `$REF`,
  so by construction they cannot come from the target. A future version's
  Steps 1-2 must stay "check the backup, take the lease and save a dirty tree,
  then resolve a ref into `$REF`", honoring a pinned message (below).
- **The target's copy is entered at Step 3**, and everything from there on --
  the worker dispatch, the report audit, and the apply (Step 5b runs the
  staged copy's `update_self.py apply`) -- is the target version's. A future
  version's Step 3 must stay the worker dispatch, under a heading an older
  SKILL.md's "follow the staged copy's `SKILL.md` from **Step 3** onward"
  finds; otherwise an older initiator handing off into a newer copy (or vice
  versa) lands at the wrong step. An initiator from before SKILL.md had its
  "Standing instructions" section has not read them, so Step 3 opens by
  sending it there.
- **The step headings are parsed.** `update_self_test.py` reads SKILL.md by
  heading (`## 2. Resolve the target`, `## 3. Dispatch the worker`) and the
  step references by file name; a renamed heading or a command moved between
  files moves its test with it.
- **Step 3a's ceiling re-check** must stay: it is the only ceiling check that
  runs whatever copy ran Step 2 (see `version-ceiling.md`, "When Step 3a
  finds the target above the ceiling").
- **Step 3a's forced tag fetch** must stay too. A copy of Step 2 whose own
  fetch is not forced cannot move a tag the workspace already holds, so Step
  3a fetches the tags again, forced, before it builds the bridge, and the
  bridge is built against the commit the worker will merge.
- **`/tmp/update-self-target.json` is written by the local copy and read by the
  target's prose**, so its writer can be several releases older or newer than
  its reader. Only `ref` is guaranteed; a version predating the file does not
  write it at all. Every other field a prose reads must be gated on its presence
  ("if and only if X is true"), never interpolated -- which is what lets a field
  be dropped without breaking the releases that still read it, and what keeps a
  field's *name* fixed once any release reads it (`exceeds_ceiling` is read by
  every release from minds-v0.3.10 on).
- **The staging path** must stay
  `data/.tasks/update-self/skill-at-target/.agents/skills/update-self`, for the
  same reason.
- **Every `update_self.py` invocation from Step 3 on uses the staged copy** (a
  test holds the prose to this). A relative
  `.agents/skills/update-self/scripts/update_self.py` resolves to the
  workspace's own copy, which may predate a subcommand the prose relies on --
  the `run-status` writes in particular fail exactly on the first update into
  the release that ships them (minds-v0.4.3, so an older copy's Step 1
  recorded no pass). The staged copy also keeps the record of the apps a pass
  stopped, which an older own copy would drop. The one exception is the
  verdict after the staged copy exits 75 (system Python older than the
  target's floor): the staged copy exits 75 on every subcommand there, so the
  command it prints for that verdict runs the local copy, and is skipped when
  the local copy predates `run-status`.
- **Every prose path outside this skill's own directory uses the staged copy**
  from Step 3 on, in SKILL.md and in every reference (a test holds the prose to
  this, and to naming only paths `stage-shared-prose` stages:
  `STAGED_PROSE_PATHS` in `update_self.py`). A bare `.agents/shared/...` path
  resolves to the workspace's own tree, which may predate the file, or hold an
  older one that contradicts this skill. Steps 1-2 run before anything is staged
  and name the workspace's copies. Scripts are not staged this way: the ones
  this skill needs from outside are mirrored into its package.
- **The task-file template keeps `lead_agent` and `finish_report_path`** (a
  test holds this too): the lead follows the staged prose but launches with
  the workspace's own `launch-task/create_worker.py`, and under a launcher
  that does not stamp the line it is the only thing that gives the worker a
  report address: the worker finishes, cannot deliver, and the lead waits out
  the timeout. Staging a newer launcher would not help: it may depend on
  settings or plugins the merge has not landed yet.
- **The prose asks of `create_worker.py` only what every supported launcher
  provides** (`python/update_self_skill/launcher_contract_test.py` pins that set to the oldest
  release the app updates from, over SKILL.md and every reference). Clearing
  the previous pass's worker uses plain `mngr list` / `mngr destroy`, and
  clearing its branch plain `git`, for this reason. A reply to a gate uses
  `reply`, which a launcher has from minds-v0.6.1 on, and `older-workspaces.md`
  has the send for one without it.
- **The worker delivers its report by hand**: the worker guide's Step 6
  writes the report file and copies it into place, which is all the
  launcher's `report` subcommand does, since a launcher from before
  minds-v0.6.2 has no `report`. A launcher from before minds-v0.6.1 stamps no
  `lead_work_dir` either, so wherever the worker guide reads `LEAD_WORK_DIR`
  it falls back to the main worktree of the same repo.
- **Teardown carries on when `workspace-layout show` fails.** A tree from
  before minds-v0.8.6 has no `show`, and a shell that is not up fails it on
  any release, so that line stays at any floor.
- **The report poll runs the staged skill's own `scripts/run_in_background.py`**,
  whose code (`python/update_self_skill/run_in_background.py`) is a
  byte-identical mirror of `system/scripts/workspace_bare_scripts/run_in_background.py`
  (a test holds the two equal), not the workspace's `system/scripts/` copy, which an
  older tree lacks. It is standard-library only, wraps the floor launcher's
  `await`, and looks up its messenger in the workspace's tree:
  `system/scripts/message_chat.py` where there is one, else `mngr message`. `python/update_self_skill/staged_runner_test.py` holds what it asks of each
  to the oldest release that has it.

## What can go as the floor rises

The floor is the oldest release a workspace updates from in one pass
(`older-workspaces.md` has its value). The items above that only an older
release needs, and when each can go:

- CLEANUP: at a floor of minds-v0.4.3 every supported copy has `run-status`.
  The "skipped when the local copy predates `run-status`" clause above goes,
  with the sentence that skips the verdict in `older-workspaces.md` ("A
  system Python older than 3.12").
- CLEANUP: at a floor of minds-v0.6.2 the worker guide's Step 6 and the task
  file in `launching-the-worker.md` may deliver through `report` as the
  shared reporting reference says, and the `LEAD_WORK_DIR` fallbacks (the
  cron listing of the worker guide's Step 4a, and its Step 6) can go.
- CLEANUP: at a floor of minds-v0.9.0 every supported Step 2 reads the pin.
  Remove `--ceiling` as a spelling of `--app-version` (`update_self.py`), and
  the app drops the `--ceiling` instruction from its starting message.
- CLEANUP: at a floor of minds-v0.10.0 `launching-the-worker.md` may use the
  launcher's own subcommands (`stop`, `destroy`) and the task file may drop
  the two frontmatter lines the launcher stamps. Update
  `launcher_contract_test.py` and
  `test_skill_md_task_template_carries_the_lead_agent_and_report_fields` with
  them. The staged runner stays while the lead launches cross-version.
- CLEANUP: remove the line naming `python-packaging-migration.md` in
  `after-the-apply.md` when `user_package_migration.py`'s own marker says
  that module can go.

## The scheduled entry: a pinned target and ceiling

The Imbue Studio app starts every pass with one message, scheduled or pressed,
that names the target and the app's ceiling, so the lead never has to reach the
app (which may be closed, on a laptop that is asleep). The app writes that
message and the template only reads it, from Steps 1 and 2 of the workspace's
*local* copy, so it is a contract with every release's Steps 1 and 2:

- **Step 2 must honor the pin in every version.** One that knows the pin reads
  its `Ceiling:`, and a `Target:` the user chose, into `resolve-target`. One
  that predates the pin (a release before minds-v0.9.0) follows the message's
  own instruction to append `--ceiling <ref>` to `resolve-target`, so
  `--ceiling` must stay an accepted spelling of `--app-version`. Neither kind
  asks the app, and Step 3a's re-check passes the same pin.
- **Step 1 must never stop to ask.** Nobody may be watching a pass, so a future
  Step 1 must keep taking an abandoned lease over by rule and saving a dirty
  tree as one commit rather than refusing it.

The `--ceiling` instruction exists only for the Step 2 copies that predate the
pin; the app drops it once the oldest release it updates from reads the pin
itself ("What can go as the floor rises").

## What the apply must tolerate

Because the apply runs from the staged copy, an old workspace updating in runs
the *target's* apply against its own pre-merge tree. Fixes to the apply flow
take effect for the very update that ships them, which also means the apply
must keep tolerating older pre-merge trees: guarded imports (the
`oom_priority` bands module is loaded off the pre-merge tree and refused
wholesale when it lacks any attribute the apply reads), no assumptions about
the pre-merge layout, and a skill directory that is staged and run as one unit
(the entry scripts put the skill's own `python/` on `sys.path` and import the
`update_self_skill` package from there, so the staged copy's own code never
resolves from the workspace's tree or venv).

A caught-up pass (SKILL.md Step 5b, the worker guide's Step 7) lands a catch-up merge
whose first parent is the worker's line and whose second parent is the
workspace's `HEAD` from before the apply. So the `update-self:` merge stays on
`HEAD`'s first-parent line, where the apply and `resolve_template_base.py` look
for it (`footprint-ranges` finds it by its subject alone), but the commits
other chats made during the pass do not. A tool that walks the first-parent
line to list the workspace's own commits misses them; find them through the
second parent of a catch-up merge instead.

## What the what's-new listing assumes of each release

`changelog-entries` runs from the target's copy against a base of any age and
lists the target's section of each release newer than the base's nearest
`minds-v*` tag. So every `minds-v*` tag's `CHANGELOG.md` holds its own section
under an empty `[Unreleased]`: a release without one shows nothing for itself,
and an `[Unreleased]` line left in a tag is shown again with the next release.

## Trust

The handoff runs the target ref's `update_self.py` and follows its prose
*before* the worker has validated anything. For the default target (a stable,
already-tested `minds-v*` tag) that is the same trust basis as the merge
itself; a `--override` to an untrusted ref means trusting that ref's code
and instructions. Only override to a ref you trust.
