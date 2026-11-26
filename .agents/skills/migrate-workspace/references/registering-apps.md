# Registering the migrated apps

Every app in this workspace is registered with `app-install`: it keeps the
app's record (`system/config/apps/<name>.toml`), gives the app its port, and
generates the app's program file and the links to the skills it carries.
Nothing here is written by hand. Run each command from the repo root of the
worktree you are migrating in; each prints one JSON line whose `result` says
what happened.

Take each migrated app by the case it is in.

## It came with a record

The old workspace already kept `system/config/apps/<name>.toml` for it. Bring
the record over with the app's directory, and leave everything generated
behind: its old program files and the `.agents/skills/` links to its skills.

```bash
uv run --no-sync app-install regenerate <name>
```

`regenerated` wrote the program files and links for this workspace
(`changed_paths`). The record keeps the port the app had; an app that shares
its port with an app already here is given a new one, named under
`reassigned_ports`. `collision` wrote nothing: a skill the app carries has
the name of one already here, which is one of the collisions the lead settled
with the user (`decisions.md`).

An app whose record says it was installed from someone else's repository
(`origin = "installed"`) runs here as it did, but cannot be updated from its
publisher: the history that connects it to that repository did not come
over. Say so in your report, and that removing it
(`uv run --no-sync app-install uninstall <name>`, which keeps its data) and
adding it again with the `install-app` skill restores that.

The record also names the old workspace's repositories for the app
(`dev_repo_url`, `published_repo_url`). They are still the user's, so leave
them, and name them in your report: the app's history in this workspace
starts here, so with GitHub sync on, the sync service's push to the old dev
repository is refused (`push_refused` in `github-sync status`) and nothing
there is overwritten. Which workspace keeps that repository is the user's
call.

## It has an `app.toml` and a hand-written program file

It was built before apps had records. Convert it, dry run first:

```bash
uv run --no-sync app-install migrate <package> --dry-run
uv run --no-sync app-install migrate <package>
```

`migrated` moved the app's routines into its directory, wrote its record with
the port its program file registered and what its manifest named under
`[[references]]`, and replaced that file with the generated one. `unconverted` changed nothing and lists why
under `problems`:

- a name the app shares with something already here: a collision the lead
  settled with the user (`decisions.md`);
- a program file that is not what the app's manifest generates: the app runs
  in a way its `app.toml` does not say yet. Declare it there (`[run]` for its
  command and arguments, `[[services]]` for a background program,
  `[[secrets]]` for a secret file it runs under) and convert it again.

## It has an `app.toml` and no program file here

```bash
uv run --no-sync app-install add <package>
```

`added` allocated its port, wrote its record, and generated its program
file. Pass `--port <the port it had>` only when the user needs the old port
kept; a taken one is refused.

## It has no `app.toml`

It predates manifests. Write one first, at `system/apps/<package>/app.toml`:
its `name`, `display_name` and `icon`, and a `[run]` table when it does not
start the way a scaffolded app does. In `[run]`, `command` is one word, the
program itself; its arguments go in `args`, and `{port:main}` in `args` or
`env` is where the app takes the port the workspace allocates. It is the
manifest the `build-app` skill writes when it wraps an existing server, and
that skill has the rest. Then `add` it as above.

## When all are registered

```bash
uv run --no-sync app-install check
```

It must answer `consistent`; `inconsistent` names each problem. A background
service with no app (one under `system/services/`) is not registered this
way and keeps its own hand-written program file.
