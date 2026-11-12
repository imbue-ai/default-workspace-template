# What the scaffolder writes

Read this when you need to change something `scaffold_flask_lib.py` wrote other
than the app's routes (its dependencies, its program command, a credential), or
when the app fails to start. `<package>` is the app's name with underscores for
hyphens.

## Contents

- The scaffolder's flags
- The app's files
- The supervisord program
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
    [--secrets-file <name>]
```

Required:

- `--name`: the app's name, to step 2's naming rule in SKILL.md.
- `--description`: the manifest's `description`; SKILL.md's step 3 says what
  to write. The visiting-agent primer shows it, the app's API document carries
  it, and it becomes the `pyproject.toml` description.
- `--icon-file`: the icon drawn in step 2 (`.svg` only). It is copied to
  `system/apps/<package>/icon.svg`, named by the manifest, and registered on
  every start.

Optional:

- `--start`: registers the app with supervisord (`reread` and `update`) and
  waits for it to answer healthy on `http://127.0.0.1:<port>/health`. Pass it:
  it saves the supervisor commands below.
- `--display-name`: the manifest's `display_name` (step 3). It defaults to
  the description.
- `--port`: an explicit port. Omitted, the scaffolder picks the lowest port at
  or above 8080 that passes its check: nothing is bound to it, neither
  `system/supervisord.conf` nor a `system/supervisord.conf.d/*.conf` file nor
  `data/.state/apps.toml` names it, and it is not `8083` (the Screen's CDP
  proxy, which is written in none of them). An explicit port is held to the
  same check. So a manual port check (`ss -tln`) is unnecessary.
- `--extra-dep`: repeatable. A library beyond `flask` and `flask-sock`
  (`--extra-dep "jinja2>=3.1" --extra-dep "anthropic>=0.40"`).
- `--secrets-file <name>`: see "An app that needs a credential".
- `--skip-uv-sync`: skip the final manifest check and the app's own lock and
  venv (for fast iteration and dry runs).

The scaffolder exits non-zero with a clear stderr message if the app's package
already exists, the name is reserved or invalid, the description is empty,
spans lines or is over 200 characters, the requested port is taken, or the
manifest check, the app's `uv lock` or its `uv sync` fails.

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

## The app's files

Everything is under `system/apps/<package>/`. No shared file is written, which
is what lets two agents scaffold two apps at once: the root `pyproject.toml`
and `uv.lock` are untouched (the root `members` names only the built-in
packages), so the app's dependencies never move the chat's or another app's.

- `app.toml` -- the app's manifest: its registered `name`, `display_name`,
  `description`, `api_schema_path`, `icon`, `priority`, `program` and
  `stop_when_no_windows`. What each field means and when to change it is in
  `app-manifest.md`.
- `icon.svg` -- the icon passed as `--icon-file`, named by the manifest and
  registered on every start.
- `pyproject.toml` -- the app is a **standalone uv project**, not a root
  workspace member. It declares `[project.scripts] <name> = "<package>.runner:main"`,
  the entry point its own sync installs into its own `.venv`. It depends on the
  workspace's shared code (`app-schema`) through an editable path source, so
  that code is shared live and `imbue-common` comes from the root's mngr pin.
  It makes the directory its own `ty` project (an empty `[tool.ty]` table and a
  pinned `ty` dev dependency) for the ratchets' type check. It carries the
  root's two-week supply-chain cooldown (`[tool.uv] exclude-newer = "14 days"`),
  which a standalone project does not inherit. Its `description` is the
  `--description` one-liner. Never add an `imbue-common` source of your own: a
  second source for the same package fails to lock.
- `uv.lock` -- the app's own lock, seeded from the root `uv.lock` so the app
  starts on the built-ins' versions and resolves from the warm cache.
- `.venv` -- the app's own venv, a link to a directory beside the uv cache
  (`system/scripts/workspace_bare_scripts/venv_links.py`).
- `src/<package>/__init__.py` -- empty.
- `src/<package>/runner.py` -- the app, a synchronous Flask starter. What it
  sets up and the rules its code follows are in `app-code.md`.
- `src/<package>/runner_test.py` -- the one test every app starts with: its API
  document at `/api/schema` says what its manifest says and covers every route
  (`api-schema.md`).
- `test_<package>_ratchets.py` -- the standard ratchets at zero, plus
  `test_no_type_errors`, which fails on any `ty` error in the app.
- `README.md` -- the one-line description.

## The supervisord program

The scaffolder writes the app's own program block to
`system/supervisord.conf.d/<name>.conf`:

```ini
[program:<name>]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project system/apps/<package> && uv sync --locked --project system/apps/<package> && python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url http://localhost:<port> && exec system/apps/<package>/.venv/bin/<name>"
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
- `bash -c "..."` is required because supervisord runs a command directly, with
  no shell, and this one chains its parts with `&&`.
- The `oom_tag_service.py user` prefix tags the app as user-created, so it is
  shed before any built-in service under memory pressure
  (`system/services/oom_priority/README.md`).

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
`connect-external-service` skill's secret card) declares it in `app.toml` and
runs under a wrapper. Pass `--secrets-file <name>` and the scaffold writes the
wrapped program command. The `[[secrets]]` block you add to the manifest by
hand either way, since only you know the variables. It names the file, its
variables, and a one-line note:

```toml
[[secrets]]
file = "example"
variables = ["EXAMPLE_API_KEY"]
note = "An Example API key from the account's settings page"
```

`publish-template` reads the block, and refuses to assemble a program that runs
under a file no block declares. The program command wraps the entry point so
the file's variables reach the process and nothing else does:

```ini
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project system/apps/<package> && uv sync --locked --project system/apps/<package> && python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url http://localhost:<port> && exec python3 system/scripts/with_secrets.py data/.secrets/example.env -- system/apps/<package>/.venv/bin/<name>"
```

A preview of the app (`update-app`'s `preview_app.py`) runs it under the same
wrapper, once per `[[secrets]]` block, reading the live `data/.secrets/`.
