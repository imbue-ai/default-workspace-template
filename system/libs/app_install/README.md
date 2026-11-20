# app_install

The `app-install` command: what registers an app a workspace's agent builds, and what keeps the files the workspace needs for it in step with its manifest.

A user app is one self-contained directory, `system/apps/<package>/`.
Everything the app owns is inside it: its code, its skills (`skills/<name>/`), its routines (`routines/<id>.toml`), and its environment units (`env.d/<NNNN>-<name>.sh`).
The command is deterministic and asks nothing; every run ends in one JSON line on stdout.

```bash
uv run --no-sync app-install add <package> [--port <n>]
uv run --no-sync app-install regenerate [<name>]
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

A port is the lowest one at or above 8080 that no supervisord program, no registry row, and no app record uses, and that nothing is bound to.
The port reaches the app through its environment, so no file in the app's directory holds it; anything that calls an app finds its URL by name (`uv run --no-sync workspace-layout list`, or the app SDK's `resolve_app_url`).
Two branches can each allocate the same port; `check` reports it and `regenerate` gives a new port to the app whose record was committed later.

An app is refused before anything is written when it would take a name something in the workspace already holds: the app's name, a program, a console script, a skill, a routine id, or an environment unit's file name.
The result names every collision.
