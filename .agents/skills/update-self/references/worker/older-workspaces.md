# Older workspaces, for the worker: what differs, and when each difference can go

Your worktree is a checkout of a workspace whose own tree, launcher and
installed tools are whatever release it is still on, while the guide you
follow is the target release's. Everything here is true only for a workspace
older than some release. Nothing else in your guide carries such text: a step
that has a difference points at its section here in one line.

Each item opens with the test for whether it applies. Its `CLEANUP:` line
names what to remove, and the floor at which that can go: the floor is the
oldest release a workspace updates from in one pass. Its value, and the
release at which every item goes, are in the lead's
`references/older-workspaces.md`.

## Contents

- A shallow clone
- Delivering the report by hand
- Cron entries from before routines
- Apps from before the app model
- Python from before the packaged layout
- A user app on the Screen's ports

## A shallow clone

**Applies when:** the git common dir holds a `shallow` file. The guarded fetch
in the worker guide's Step 1 tests it for itself.

A workspace made from a pool host baked before the full-history bake fix
carries a `--depth 1` clone, in which `git log` dead-ends at a parentless
graft commit and "what changed since the fork point" has no answer. The fetch
completes the history. The marker is repo-wide state in the common dir, which
your worktree shares, so the call heals the whole workspace, and it is a
no-op when the lead's already did. A clone whose boundary predates the
template's history rewrite stays shallow after the fetch: the lead's bridge
is what gives it a merge base, and nothing more is asked of you.

CLEANUP: remove this section and the guarded fetch in the worker guide's Step
1 once no supported workspace predates the bake fix.

## Delivering the report by hand

**Applies when:** on every pass, since the launcher in your checkout is the
workspace's own whatever its release. `LEAD_WORK_DIR` coming back unset from
the task file means that launcher stamps no `lead_work_dir` either.

The shared reporting reference delivers a report with the
launcher's `report` subcommand, which a launcher from before minds-v0.6.2
does not have. So the worker guide's Step 6 writes the report file and copies it into
place itself, which is all `report` does. A launcher from before minds-v0.6.1
also stamps no `lead_work_dir`, so `LEAD_WORK_DIR` may be unset: the lead's
checkout is then the main worktree of this same repo.

CLEANUP: at a floor of minds-v0.6.2, the worker guide's Step 6 and the task
file in `launching-the-worker.md` may deliver through `report` as the shared
reference says, and the `LEAD_WORK_DIR` fallbacks (the cron listing of the
worker guide's Step 4a, and its Step 6) can go.

## Cron entries from before routines

**Applies when:** the listing of the lead's `data/.state/cron.d` in the worker
guide's Step 4a shows an entry.

From minds-v0.9.0 a scheduled task is a routine, a tracked
`system/routines/<id>.toml`. `routines migrate-cron` moved the cron drop-ins
of older workspaces across, but may have left one in place. Those live under
the gitignored `data/.state/cron.d`, so git shows none of them and your
worktree holds none (the launcher syncs only this pass's
`data/.tasks/update-self/` into it). So the worker guide's Step 4a lists the
lead's copy on every pass, `$LEAD_WORK_DIR/data/.state/cron.d`, and greps any
entry it holds for every retired name and every deleted or renamed path, as
it greps the routines. Without `LEAD_WORK_DIR` it lists the main worktree of
this same repo, since a directory you cannot read answers exactly like a
workspace with no such entry. A hit is a user-created consumer, and the
impact analysis runs in full.

CLEANUP: remove this section and the cron listing in the worker guide's Step
4a and in `worker/impact-analysis.md` once no supported workspace holds a
cron drop-in (a floor of minds-v0.9.0, and a fleet check for leftovers).

## Apps from before the app model

**Applies when:** an app directory under `system/apps/` has no `app.toml`. The
`--package app-manifest` form is on every call whatever the workspace holds.

From minds-v0.5.1 an app has an `app.toml` and the `app-manifest` library
computes its footprint. Two leftovers of the time before:

- Your `app-manifest` calls carry `--package app-manifest`, which
  installs the library from the merged tree. A workspace from before the app
  model has none of it, and the root project does not depend on it.
- An app directory with no `app.toml` has no footprint the library can
  compute. Its directory is its footprint: read `git diff` over it by hand for
  the two ranges `footprint-ranges` names.

CLEANUP: remove the second item, and its pointer in
`worker/impact-analysis.md`, once no supported workspace holds an app without
a manifest (a floor of minds-v0.5.1, and a fleet check). The `--package` form
stays while you read the merged tree's library.

## Python from before the packaged layout

**Applies when:** `system/test_user_packages_are_standalone.py` names a user
package, or the environment gate or a suite fails on the workspace's own
skills, apps, services or docs over a rule the update's changelog changed.

minds-v0.10.0 moved every skill's Python into a package, made each user app,
service and lib a standalone uv project, and changed how commands are launched. The rules
themselves are in
`data/.tasks/update-self/skill-at-target/.agents/shared/references/running-python.md`
and
`data/.tasks/update-self/skill-at-target/.agents/shared/references/spec-summary.md`
("Packaging"). A workspace updating across that release has files of the
user's in the old layout, and
`data/.tasks/update-self/skill-at-target/.agents/shared/references/python-packaging-migration.md`
is the guide to converting them:

- **A user package that is not standalone yet**
  (`system/test_user_packages_are_standalone.py` names it) is converted with
  `user-package-migration`'s `migrate`, after its `--dry-run`, whose output
  goes in your report, per that guide ("A user app, service or lib").
  Each converted package then gets your suites, and an app or service its
  isolated boot, from its own venv.
- **A failure in your environment gate or suites** on the workspace's
  own skills, apps, services or docs that the update's changelog ties to a
  changed rule is fixed by following that guide. Examples: a user skill's PEP
  723 script that `system/test_entry_point_import_cost.py` cannot import, a
  user service whose program line runs under `uv run`, a type error in a
  user skill or app.

CLEANUP: this section, the `user-package-migration` command, the item that
names them in `worker/validation.md` and the sentence naming them in the
worker guide's Step 4b go when
`user_package_migration.py`'s own marker says that module can. The guide is a shared file other skills read, so
it goes when the last of them stops naming it.

## A user app on the Screen's ports

**Applies when:** a workspace-added app's program line (`--url`) or its
runner's `PORT` default uses port 8090 or 8091.

Those are the Screen's service and neko ports, which older scaffolds could
hand out, breaking the Screen. Move the app to a free port in your branch
(its program line's `--url` and its runner's `PORT` default), and name the
move under **Impact analysis**.

CLEANUP: remove this section and its pointer in `worker/impact-analysis.md`
once the floor reaches the first release whose pass carries this item: every
workspace has then been through one.
