# app_install

The `app-install` command: what registers an app a workspace's agent builds, and what keeps the files the workspace needs for it in step with its manifest.

A user app is one self-contained directory, `system/apps/<package>/`.
Everything the app owns is inside it: its code, its skills (`skills/<name>/`), its routines (`routines/<id>.toml`), and its environment units (`env.d/<NNNN>-<name>.sh`).
The command is deterministic and asks nothing; every run ends in one JSON line on stdout.

```bash
uv run --no-sync app-install add <package> [--port <n>]
uv run --no-sync app-install install <url> [--branch <ref>]
uv run --no-sync app-install update <name> [--branch <ref>] [--continue]
uv run --no-sync app-install regenerate [<name>]
uv run --no-sync app-install uninstall <name>
uv run --no-sync app-install migrate <package> [--dry-run]
uv run --no-sync app-install check
uv run --no-sync app-install list
```

## The app record

Each user app has one tracked file, `system/config/apps/<name>.toml`: the app's name, its directory, the loopback port of its main program, whether this workspace built it (`origin = "local"`) or installed it, and the repositories it is kept in step with.
It is one file per app so that two branches that each add an app never edit the same file.
An app under `system/apps/` without a record is a built-in app.

## Derived files

The workspace needs two kinds of file outside an app's directory, and both are generated from the app's manifest and record rather than written by hand:

- `system/supervisord.conf.d/<program>.conf`: one file per program. The app's main program runs the manifest's `[run]` command (by default the console script named after the app) with the app's port in its environment, after registering the manifest and the port through `forward_port.py`. Each `[[services]]` entry is the program `<name>-<role>`. A program of a Python app first links and syncs the app's own virtual environment from its lock, and a declared secret file reaches the program through `with_secrets.py`.
- `.agents/skills/<skill>`: a symlink to each skill the app carries, which is how a harness discovers it.

They are tracked in the workspace repository and never enter an app's own repository.
Do not edit one: change the manifest and run `app-install regenerate`.
`app-install check` fails when one differs from what the generator writes, and `system/test_app_manifests.py` runs it.

## Ports and names

A port is the lowest one at or above 8080 that no supervisord program, no registry row, and no app record uses, that is not one of the two ports the Screen holds without a program line (8083 and 8091), and that nothing is bound to.
The port reaches the app through its environment, so no file in the app's directory holds it; anything that calls an app finds its URL by name (`uv run --no-sync workspace-layout list`, or the app SDK's `resolve_app_url`).
Two branches can each allocate the same port; `check` reports it and `regenerate` gives a new port to the app whose record was committed later.

An app is refused before anything is written when it would take a name something in the workspace already holds: the app's name, a program, a console script, a skill, a routine id, or an environment unit's file name.
The result names every collision.

## Removing an app

`app-install uninstall <name>` removes the app's directory, its record, and its derived files, has supervisord drop its programs, removes its registry row, and commits the removal under the installer's own author.
It keeps the app's stored data (`data/.apps/<name>/`) and its secret files, and names them in its result.
What it could not do outside the tree, such as reaching a supervisord that is not running, is listed under `problems`; the app is removed from the tree all the same.

## Converting an app built before this layout

`app-install migrate <package>` turns an app that has a manifest and a hand-written program file, and keeps its skills and routines elsewhere in the workspace, into a user app.

- A skill the manifest's `[[references]]` names moves into the app's `skills/`, with a link left where it was, unless another app names it too or it has a Python project of its own; those, and every other reference, are listed in the record's `references`.
- A routine whose `app` is this app moves into the app's `routines/`.
- The manifest loses `[[references]]` and `[wiring]`, and its `[scope] exclude` globs become relative to the app's directory.
- The record keeps the port the app already runs on, and the program file is replaced by the generated one.

A conversion either completes or changes nothing.
An app whose existing program file is not what its manifest generates (it runs under a secret file the manifest does not declare, say, or with a command of its own) is left exactly as it was, and the result says where the two differ: declare how the app runs in `app.toml` (`[run]`, `[[services]]`, `[[secrets]]`) and convert it again.
`--dry-run` prints what would happen.

## Installing an app from a repository

`app-install install <url>` adds an app someone published to the workspace.
It takes the tag or branch named by `--branch`, else the repository's highest `v<major>.<minor>.<patch>` tag, else its `main` branch, and refuses a repository with no `app.toml` at its root (`not_an_app`).

The app enters the workspace repository with `git subtree add --squash`, so the publisher's history stays out, in a worktree of the installer's own; its record and derived files are written there, and the result reaches the primary checkout as one landed commit.
A refusal, such as a collision with something already in the workspace, leaves the primary checkout untouched, and so does the user's uncommitted work.
The record keeps the repository's URL, the commit that was taken, that commit's tree, and the app's version, and the ref `refs/app-upstream/<name>` keeps the commit's objects in the repository.

The repository is read anonymously first, and with the workspace's GitHub access when that is refused; `needs_github_access` means neither reached it and the access has to be granted first.

After landing, the command installs the system packages and tools the app's `[environment]` declares, has supervisord start the app's programs, and waits for the app's port to answer.
Its result lists what the app still needs from the user under `needs`: the permissions and secret files it declares (and whether each file exists yet), whether it calls a model, its adaptation notes, the apps it expects that are not here, and anything of its environment that could not be installed.
A program that runs under a secret file that does not exist yet cannot start, so its port is not waited for.

## Updating an installed app

`app-install update <name>` brings a newer version of an installed app in from the repository it came from, over the workspace's own changes to the app, with `git subtree merge --squash`.
The live app is untouched until the result lands.

- The app's stored data is copied to `data/.state/app_install/snapshots/<name>/` first, out of the backup, and only the newest copy is kept. When the data does not fit on the disk beside itself, the update goes ahead only if the workspace has a recent backup (`no_snapshot` otherwise).
- A conflict in `uv.lock` alone is settled by taking the publisher's lock and making it again. Any other conflict ends the run with `conflict`, the files, and the worktree the merge waits in, `data/.state/app_install/worktrees/<name>`: resolve it there, commit, and run the update again with `--continue`.
- The merged app is registered and regenerated on the port it already has, and its checks are run (its environment syncs from its lock and its tests pass). `tests_failed` leaves the worktree for a fix, and `--continue` picks it up.
- Then the result lands as one commit, the record takes the new commit, tree, and version, anything new in `[environment]` is installed, and the app's programs are restarted.

`up_to_date` means the app is already at the version asked for.
An app this workspace built has nothing to update from.
