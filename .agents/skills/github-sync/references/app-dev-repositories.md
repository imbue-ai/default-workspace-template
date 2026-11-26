# App dev repositories

The private repository an app can have of its own: what it is, how it is
created and kept current, what to tell the user before they share one, and
what each state in the sync status means.

## Contents

- What a dev repository is
- What keeps it going
- Which apps have one
- When sync is enabled
- One app, without syncing the workspace
- Giving someone access to a dev repository
- What the status says about each app
- A dev repository the user deleted
- When sync is disabled

## What a dev repository is

An app this workspace built or installed (one with a record under
`system/config/apps/`) lives in the workspace's repository, at
`system/apps/<package>/`. It can also have a repository of its own, its **dev
repository**:

- It is named `<name>-dev`, it is always **private**, and it holds the app's
  directory at its root with the app's own history: every workspace change
  that touched the app, as a change of the app's.
- It is written automatically. Nothing is pushed to it by hand, and nothing
  that lands in it comes back into the workspace.
- It is for the user and the people they choose to read and work on the app
  as a small repository of its own. It is not what other people install from:
  that is the app's published repository, which exists only once the user
  publishes the app (the `publish-app` skill).

## What keeps it going

Two things keep it going, and they are separate on purpose:

- **`app-install link <name>`**, which an agent runs, does what must happen
  once: it gives the app's history a starting point in the workspace (so
  later passes take a fraction of a second rather than minutes) and creates
  the repository. Its result is `linked`, `refreshed` or `already_linked`,
  with `dev_repo_url`, `is_dev_repository_created`, `remaining` (what is left
  before the app is synced, in sentences) and `problems` (what it tried and
  could not do). A repository that could not be created, because the account
  already has a `<name>-dev` or a GitHub permission is missing, does not undo
  the rest: fix the cause and run `link` again. `refused` changed nothing;
  `detail` says why (run outside the live workspace's root, an app with no
  saved change yet, an installed app nobody has changed).
- **The service** then compares the app's directory with what it last pushed,
  on every pass, and pushes what is new. It never creates a repository, never
  commits to the workspace, and never forces a push.

## Which apps have one

- **An app built here** gets one when sync is enabled ("When sync is enabled" below), and a new
  app gets one when it is built while sync is on (the `build-app` skill runs
  `link` after the app's first commit).
- **An installed app** has none while it is as it came: it follows the
  repository it was installed from. Once this workspace changes it (`state`
  is `modified` in `app-install list`), `app-install link <name>` gives it one
  the same way, and its history there continues from the publisher's.

## When sync is enabled

`uv run --no-sync app-install list` names every app built or installed here.
For each one whose `state` is `owned` (built here) or `modified` (installed,
then changed here), run, from the repo root of the live workspace and never
from a worker's worktree:

```bash
uv run --no-sync app-install link <name>
```

It creates the private repository `<name>-dev` under the user's own GitHub
account (theirs even when the sync repo sits under an organization), records
its URL in the app's record, and commits that one file; the service then
fills the repository on its next pass and keeps it current. Skip an app whose
`state` is `installed`: one that is still as it came has no dev repository,
and `link` refuses it. Read each result as "What keeps it going" says, and
carry on past an app whose repository could not be created; the workspace's
own sync does not depend on it.

## One app, without syncing the workspace

When the user wants one app in a private repository of its own and has not
enabled GitHub sync, file the two permission requests of SKILL.md's Enable step 2
(saying they are for this app's repository), then:

```bash
uv run --no-sync app-install link <name> --create-dev-repo
uv run --no-sync github-sync sync-app <name>
```

The first creates the repository; the second pushes the app once and prints
one JSON line whose `result` is one of the states below (it exits nonzero
unless that is `pushed` or `up_to_date`). Nothing keeps that repository
current afterwards: run `sync-app` again after later changes, and tell the
user that this copy is as of the last time it was brought up to date.

## Giving someone access to a dev repository

A user who wants particular people to see or work on an app without
publishing it can add them to its dev repository, in that repository's
settings on GitHub. Before they do, tell them plainly what those people will
see, because it is more than the app as it is today:

- **Nothing has scanned it for secrets.** The scan that guards a published
  app has never run on a dev repository. A password, key or token that was
  ever saved in one of the app's files is in its history, even if it was
  removed later.
- **It carries the message of every workspace change that touched the app**,
  as it was written. A change that also touched something else is described
  in full, so a message can mention the user's other apps or other work.
- **It is the whole history**, not one clean version: every intermediate
  state of the app is there to read.

To share an app with people who should see none of that, publish it instead
(`publish-app`).

## What the status says about each app

`uv run --no-sync github-sync status` lists every app under `service.apps`,
each with a `state`, its `dev_repo_url`, `visibility`, `pushed_commit` and a
`detail` saying why. `service.app_pass_error` is set when the pass did not
run at all.

| `state` | Meaning | What to do |
| --- | --- | --- |
| `pushed` | The app's new changes were pushed on this pass. | Nothing. |
| `up_to_date` | The repository already holds the app as it is. | Nothing. |
| `unmodified` | An installed app nobody has changed; it has no dev repository by design. | Nothing. |
| `no_dev_repository` | An app built here, or a changed installed app, whose record names no repository yet. | Run `uv run --no-sync app-install link <name>`. |
| `not_linked` | The app has a repository but its history has no starting point, so the pass skips it rather than spend minutes on it. | Run `uv run --no-sync app-install link <name>`, from the live workspace's root. |
| `visibility_unknown` | The repository has not been confirmed private yet, so pushes wait. It clears by itself once GitHub answers. | If it stays, check the GitHub permissions are still granted, and that the repository still exists. |
| `not_private` | The repository is public. Its pushes are stopped; the workspace's own sync and every other app carry on. | Tell the user it has become public and that anyone can read its whole history, and ask them to make it private again in its settings on GitHub. Pushes resume by themselves within about fifteen minutes. Never work around the stop. |
| `push_refused` | The repository holds history this workspace's copy of the app does not continue, as when a second workspace (one restored from a backup, say) pushes the same app to it. Nothing was overwritten, and nothing will be. | Tell the user two workspaces are writing to one copy, and ask which one should keep it. In the other, the copy stays as it is; do not try to force it. |
| `failed` | Anything else that stopped this app; `detail` has the reason. | Act on `detail`. The pass tries again each time. When `detail` says the app's history could not be read in the time a pass is given and names `app-install link <name> --refresh`, run exactly that from the live workspace's root: so much has changed in the workspace since the app was last linked that reading it all takes too long, and the command gives the app a new starting point at the present. |

An app whose `failed` detail says its directory "was removed and made again"
cannot have a dev repository: its history would carry the workspace's own
changes and files along with the app's, so nothing is ever pushed for it. Say
so plainly if the user asks why that app has no copy; there is no command
that changes it.

## A dev repository the user deleted

A dev repository the user deleted on GitHub is never recreated behind their
back: its pushes fail and the status says so. If they ask for it back, create
an empty private repository of the same name under the same account (as
SKILL.md's Enable step 4 does), delete `data/.state/app_sync/<name>.json` so the next
pass pushes everything again, and check the status.

## When sync is disabled

The apps' dev repositories stay on GitHub, each still named in its app's
record, and stop being updated. Leave them unless the user asks for them to
be deleted too; the `DELETE` that SKILL.md's Disable uses for the sync
repository removes one. One the user still wants kept current is brought up
to date on request with `uv run --no-sync github-sync sync-app <name>`.
