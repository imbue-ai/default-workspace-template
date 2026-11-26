# What the scaffolder writes

Read this when you need to change something `scaffold_flask_lib.py` wrote other
than the app's routes (its dependencies, how its program runs, a credential),
or when the scaffolder or the app's start fails. `<package>` is the app's name
with underscores for hyphens.

## Contents

- The scaffolder's flags
- When the scaffolder fails
- The app's files
- The record and the supervisord program
- How the app starts
- Dependencies and the app's venv
- An app that needs a credential

## The scaffolder's flags

```bash
uv run --no-sync .agents/skills/build-app/python/build_app_skill/scaffold_flask_lib.py \
    --name <name> \
    --description "<one-liner>" \
    --icon-file <path-to-svg> \
    --start \
    [--display-name "<what users see>"] \
    [--port <int>] \
    [--extra-dep <pkg>] [--extra-dep <pkg>] ... \
    [--secrets-file <name> --secrets-variable <NAME> [--secrets-variable <NAME>] ...]
```

The scaffolder writes the app's directory and then registers it through
`app-install`, which allocates the app's port, writes its record, and
generates its supervisord program file.

Required:

- `--name`: the app's name, to step 2's naming rule in SKILL.md. Registering
  the app also refuses a name that another app, a program in
  `system/supervisord.conf` or any `system/supervisord.conf.d/*.conf`, or a
  console script already holds. And the name must not be another app's name
  with a hyphen and more after it, nor may another app's name be this one
  extended that way: beside the built-in `chat`, `chat-export` is refused, and
  so is `notes` beside an existing `notes-sync`. An app's background programs
  are named `<name>-<role>`, so one of the two apps would be taken for a
  program of the other.
- `--description`: the manifest's `description`; SKILL.md's step 3 says what
  to write. The visiting-agent primer shows it, the app's API document carries
  it, and it becomes the `pyproject.toml` description.
- `--icon-file`: the icon drawn in step 2 (`.svg` only). It is copied to
  `system/apps/<package>/icon.svg`, named by the manifest, and registered on
  every start.

Optional:

- `--start`: has supervisord pick up the app's generated program (`reread` and
  `update`) and waits for it to answer healthy on
  `http://127.0.0.1:<port>/health`. Pass it: it saves the supervisor commands
  below.
- `--display-name`: the manifest's `display_name` (step 3). It defaults to
  the description.
- `--port`: the port to register the app on. **Do not pick one: the workspace
  allocates it.** Omitted, `app-install` gives the app the lowest port at or
  above 8080 that no supervisord program, no row of `data/.state/apps.toml`
  and no other app's record holds, that is not one of the Screen's ports, and
  that nothing is listening on, so a manual port check (`ss -tln`) is
  unnecessary. The port is written to the app's record and reaches the app
  through its program's environment; no file in the app's directory holds it.
  Pass `--port` only when the user needs a particular port; a taken one is
  refused.
- `--extra-dep`: repeatable. A library beyond `flask` and `flask-sock`
  (`--extra-dep "jinja2>=3.1" --extra-dep "anthropic>=0.40"`).
- `--secrets-file <name>` and `--secrets-variable <NAME>`: see "An app that
  needs a credential".
- `--skip-uv-sync`: skip the app's own lock and venv (for fast iteration and
  dry runs). The app is still registered.

With `--start`, the app is registered and running healthy when the command
returns. Without it, tell supervisord to pick up the new program:

```bash
supervisorctl reread && supervisorctl update
supervisorctl status <name>
```

`update` does not wait for the start, so a healthy new app reads `STARTING` for
its first 30 seconds. That is the `startsecs` window the program block sets,
not a failure. `BACKOFF` or `FATAL` is the failure signal, and a broken app
reaches it well before the window is up. On either, read its log
(`/var/log/supervisor/<name>-stderr.log`) or run
`supervisorctl tail <name> stderr`.

## When the scaffolder fails

The scaffolder exits non-zero with a clear stderr message if the app's package
already exists, the name is reserved or invalid, the description is empty,
spans lines or is over 200 characters, a secrets file is named without a
variable, the workspace refuses to register the app, or the app's `uv lock` or
its `uv sync` fails.

- **A refused registration** leaves nothing behind. It is refused when the
  app's manifest is not valid, when the app would take a name, a program, a
  console script or a port something in the workspace already holds, or when
  its name and another app's differ only by a hyphenated suffix. The message
  names every problem, and the app's directory is removed again, so fix what
  it names and run the scaffolder again.
- **A failure after the registration** (the lock, the sync) leaves the app
  registered, with its directory, its record and its generated program in
  place, and a second run is refused because the directory exists. Remove the
  app with `uv run --no-sync app-install uninstall <name>`, fix what the
  message names, and run the scaffolder again.

## The app's files

Everything the app owns is under `system/apps/<package>/`. No shared file is
written, which is what lets two agents scaffold two apps at once: the root
`pyproject.toml` and `uv.lock` are untouched (the root `members` names only
the built-in packages), so the app's dependencies never move the chat's or
another app's.

- `app.toml` -- the app's manifest: its registered `name`, `display_name`,
  `description`, `api_schema_path`, `icon`, `priority`, `program` and
  `stop_when_no_windows`. What each field means and when to change it is in
  `.agents/shared/references/apps/manifest.md`.
- `icon.svg` -- the icon passed as `--icon-file`, named by the manifest and
  registered on every start.
- `pyproject.toml` -- the app is a **standalone uv project**, not a root
  workspace member. It declares `[project.scripts] <name> = "<package>.runner:main"`,
  the entry point its own sync installs into its own `.venv`. It depends on
  `app-schema` through an editable path source, which is how the app SDK
  (`imbue.app_sdk`, what `runner.py` imports) and `imbue-common` reach the
  app's venv at the commit the workspace pins mngr to.
  It makes the directory its own `ty` project (an empty `[tool.ty]` table and a
  pinned `ty` dev dependency) for the ratchets' type check. It carries the
  root's two-week supply-chain cooldown (`[tool.uv] exclude-newer = "14 days"`),
  which a standalone project does not inherit. Its `description` is the
  `--description` one-liner. Never add an `imbue-app-sdk` or `imbue-common`
  source of your own: a second source for the same package fails to lock.
- `uv.lock` -- the app's own lock, seeded from the root `uv.lock` so the app
  starts on the built-ins' versions and resolves from the warm cache.
- `.venv` -- the app's own venv, a link to a directory beside the uv cache
  (`system/scripts/workspace_bare_scripts/venv_links.py`).
- `src/<package>/__init__.py` -- empty.
- `src/<package>/runner.py` -- the app, a synchronous Flask starter. What it
  sets up and the rules its code follows are in
  `.agents/shared/references/apps/code.md`.
- `src/<package>/runner_test.py` -- the one test every app starts with: its API
  document at `/api/schema` says what its manifest says and covers every route
  (`.agents/shared/references/apps/api-schema.md`).
- `test_<package>_ratchets.py` -- the standard ratchets at zero, plus
  `test_no_type_errors`, which fails on any `ty` error in the app.
- `README.md` -- the one-line description.

Outside the app's directory, every run sets one marker in the workspace's
state: `data/.state/onboarding_activity/app_build_started`, an empty file
carrying that run's time. The chat reads it so that it does not suggest
building an app while one is being built. It is not a tracked file, setting it
twice is harmless, and a run that cannot write it prints a warning and carries
on.

## The record and the supervisord program

Registering the app writes two files outside its directory, each the app's
own:

- `system/config/apps/<name>.toml`, the app's record: its name, its directory,
  the port it was allocated, and that this workspace built it. `app-install`
  keeps it; do not edit it by hand.
- `system/supervisord.conf.d/<name>.conf`, the app's program file, which
  `app-install` generates from `app.toml` and the record:

```ini
# Generated by app-install from system/apps/<package>/app.toml and the app's record.
# Do not edit this file: change the manifest and run `uv run --no-sync app-install regenerate`.
[program:<name>]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project system/apps/<package> && uv sync --locked --project system/apps/<package> && python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url http://localhost:<port> && exec system/apps/<package>/.venv/bin/<name>"
environment=<PACKAGE_UPPER>_PORT="<port>"
directory=/home/user/workspace
autostart=true
autorestart=true
startsecs=30
startretries=5
# plus rotated stdout/stderr logfiles under /var/log/supervisor/<name>-*.log
```

- `startsecs` and `startretries` bound a crash loop. An app that dies before it
  has stayed up 30 s counts as a failed start, so supervisord backs off and
  ends in `FATAL` instead of restarting a broken app several times a second for
  the life of the workspace. Built-in services deliberately retry forever
  instead.
- The command sits in `bash -c "..."` because supervisord runs a command
  directly, with no shell, and this one chains its parts with `&&`.
- The `oom_tag_service.py user` prefix tags the app as user-created, so it is
  shed before any built-in service under memory pressure
  (`system/services/oom_priority/README.md`).
- `forward_port.py` registers the app's port in `data/.state/apps.toml`; its
  flags are in `forward-port-cli.md`.

**The program file, and any `.agents/skills/` link for the app, are generated:
never edit one by hand.** To change how the app runs, change `app.toml`
(`[run]`, `[[services]]`, `[[secrets]]`) and run
`uv run --no-sync app-install regenerate <name>`, then
`supervisorctl reread && supervisorctl update`.
`.agents/shared/references/apps/manifest.md` ("How the app runs") has the
tables and their rules, among them that a `command` is one word and that a
`[run]` table added to a scaffolded app must pass the port on itself.

## How the app starts

Each start runs the command's four parts in order:

1. Link the app's venv. A rebuilt root filesystem loses the venv's directory,
   and uv refuses a dangling link.
2. Sync the venv from the app's own lock with `--locked` (about 30 ms when it
   is current, and no network).
3. Register the app from its manifest with `forward_port.py --manifest`.
4. `exec` the entry point from the app's own `.venv/bin`, not the name under
   `uv run`, so the app is the process supervisord tagged and not a child of a
   wrapper.

A lock that no longer matches the app's `pyproject.toml` (after a hand edit),
or a failed sync, fails the start with uv's error in the app's stderr log
(`/var/log/supervisor/<name>-stderr.log`), and fails nothing else. Fix the lock
(`uv lock` in the app's directory) and restart.

## Dependencies and the app's venv

The scaffold's `uv sync --locked` installs the package (editable) and its
`<name>` entry point into the app's `.venv`, which is what the program line
runs. The app's tests run from it too: the sync installs the `dev` group.

- Add a dependency with `uv add <package>` **in the app's directory**. That
  changes only the app's `pyproject.toml`, `uv.lock` and venv, never the root
  lock or the chat's environment.
- Run one of the app's commands once with
  `uv run --no-sync --project system/apps/<package> <command>`.

## An app that needs a credential

An app that needs a credential (an API key the user supplied through the
`connect-external-service` skill's secret card) declares it in `app.toml`, and
its generated program runs under a wrapper. Pass `--secrets-file <name>` with a
`--secrets-variable <NAME>` for each variable the file sets, and the scaffold
writes the `[[secrets]]` block; add its `note` by hand. For an app that already
exists, add the whole block and run
`uv run --no-sync app-install regenerate <name>`. The block names the file, its
variables, and a one-line note:

```toml
[[secrets]]
file = "example"
variables = ["EXAMPLE_API_KEY"]
note = "An Example API key from the account's settings page"
```

`publish-template` reads the block, and refuses to assemble a program that runs
under a file no block declares. The generated command wraps the entry point
once per block, so the file's variables reach the process and nothing else
does:

```ini
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project system/apps/<package> && uv sync --locked --project system/apps/<package> && python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url http://localhost:<port> && exec python3 system/scripts/with_secrets.py data/.secrets/example.env -- system/apps/<package>/.venv/bin/<name>"
```

A preview of the app (`update-app`'s `preview_app.py`) runs it under the same
wrapper, once per `[[secrets]]` block, reading the live `data/.secrets/`.
