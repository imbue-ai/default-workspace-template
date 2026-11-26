# The app's manifest (`app.toml`)

Every app has a manifest at `system/apps/<package>/app.toml`. This file covers
what the scaffolder writes there, `stop_when_no_windows`, how the app runs and
the files generated from that, what belongs in the app's directory, a pinned
window, and renaming the app.

The app's program registers the manifest at every start, so a restart is what
applies an edit. After editing it, check it:

```bash
uv run --no-sync app-manifest validate-manifest system/apps/<package>/app.toml
```

## Contents

- What the scaffold writes
- `stop_when_no_windows`
- How the app runs
- What the app owns, and what only uses it
- A pinned window (`[pin]`)
- Renaming an app

## What the scaffold writes

- `name`: the app's registered name.
- `display_name`: what users see for the app.
- `description`: the one-liner passed as `--description`.
- `api_schema_path`: `/api/schema`, where the app describes its own API
  (`api-schema.md`).
- `icon`: the icon file beside the manifest.
- `priority = "user"`: the app is shed before any built-in under memory
  pressure.
- `program`: its supervisord program.
- `stop_when_no_windows = true`: see the next section.
- A `[preview]` table: how `update-app`'s `preview_app.py` boots a throwaway
  instance of the app, on a free port over a scratch copy of its data.

## `stop_when_no_windows`

With `true`, the shell stops the app's program a minute after the app's last
window closes and starts it again on the next request. With `false`, the app
keeps running for the life of the workspace. A manifest without the field reads
as `false`.

- **Leave it `true` for an app that only answers requests.** The stop frees the
  whole process while nobody looks at it, its data on disk is untouched, and
  the next request wakes it.
- **Set it to `false` when the app does work between requests** that a stop
  would lose or interrupt: a background thread that keeps something live, a
  poller or file watcher, a websocket or subscription to an outside service, a
  job that must finish after the user closes the window, or an API another
  agent drives with no window open. A stopped app runs nothing until its next
  request, and the wake serves that request, not the work that was in flight.
- **Work on a cadence is never a reason to keep the app up**, and never a
  thread or scheduler inside it. A refresh every N minutes or a nightly sync is
  a routine with `--app <name>` (the `manage-routines` skill), which the
  Routines app shows and the user can run, pause and retime.
- The reverse holds: an app that no longer does anything between requests goes
  back to `true`.

## How the app runs

An app the workspace built or installed has a record,
`system/config/apps/<name>.toml`, which `app-install` writes when the app is
registered and keeps afterwards: its name, its directory, the port it was
allocated, and where it came from. A built-in app has no record, and this
section is not about one.

Two kinds of file are generated from the manifest and the record: the app's
program files under `system/supervisord.conf.d/` (one for the app, one for
each background program it owns), and the `.agents/skills/` link to each skill
the app carries. A generated program file begins with a comment saying so.
**Never edit a generated file or the record by hand.** To change how the app
runs, change the manifest:

- `[run]`: the command that starts the app, its `args` and its `env`.
- `[[services]]`: a background program the app owns, run as `<name>-<role>`
  (`role`, `command`, `args`, `env`).
- `[[secrets]]`: a secret file the app's program runs under (`file`,
  `variables`, `note`).

Then regenerate, and have supervisord pick the change up:

```bash
uv run --no-sync app-install regenerate <name>
supervisorctl reread && supervisorctl update
```

- **A `command` is one word**, the program itself: its arguments go in `args`.
  In an app with a `pyproject.toml` it names one of the app's console scripts
  (`[project.scripts]`), which the program runs from the app's own
  `.venv/bin`. Only an app with no `pyproject.toml` (a wrapped server) has its
  command run as given.
- **`{port:main}`** in `args` or `env` is the port the workspace allocated,
  and `{host}` the loopback address. The port is in the record and reaches the
  app through its program, so no file in the app's directory holds it.
- **A scaffolded app has no `[run]` table.** Without one, the program runs the
  console script named after the app with `<PACKAGE_UPPER>_PORT` set to its
  port. A `[run]` table replaces all of that, so one you add to a scaffolded
  app must pass the port on itself:
  `env = { <PACKAGE_UPPER>_PORT = "{port:main}" }`.
- `uv run --no-sync app-install check` fails when a generated file differs
  from what its manifest generates, which is how a hand edit is caught.

A credential the app needs (an API key the user supplied through the
`connect-external-service` skill's secret card) is one `[[secrets]]` block,
naming the `data/.secrets/<file>.env` file, each variable it sets, and a
one-line note for someone installing the app:

```toml
[[secrets]]
file = "example"
variables = ["EXAMPLE_API_KEY"]
note = "An Example API key from the account's settings page"
```

The generated command wraps the app's entry point in
`system/scripts/with_secrets.py` once per block, so the file's variables reach
the process and nothing else does. `publish-template` reads the block, and
refuses to assemble a program that runs under a file no block declares.

## What the app owns, and what only uses it

For an app with a record, `system/apps/<package>/` is the whole app: one
directory that can be shared (the `publish-app` skill), installed in another
workspace (`install-app`), and removed in one piece. Put everything that
belongs to the app inside it:

- **A skill that belongs to the app** (one whose job is to drive this app) goes
  at `system/apps/<package>/skills/<skill-name>/SKILL.md`. Then run
  `uv run --no-sync app-install regenerate <name>`, which links it into
  `.agents/skills/<skill-name>` so it is discovered like any other skill; the
  link is generated, so never create or edit it by hand. Such a skill carries
  no `python/` project of its own. Code it needs lives in the app's package
  and is reached as one of the app's console scripts (`[project.scripts]` in
  the app's `pyproject.toml`, run with
  `uv run --no-sync --project system/apps/<package> <command>`) or over the
  app's HTTP API.
- **A routine that belongs to the app** is added with
  `uv run --no-sync routines add <id> --app <name> ...` (the `manage-routines`
  skill) and lands in `system/apps/<package>/routines/<id>.toml`.
- **An environment unit** (a script that installs a system package or a tool
  the app needs) goes at `system/apps/<package>/env.d/<NNNN>-<name>.sh`, with
  `NNNN` at 2000 or higher. env-converge runs every unit at each boot, not
  when its file appears, so run a new one by hand once
  (`bash system/apps/<package>/env.d/<NNNN>-<name>.sh`, from the repo root)
  for the app to have what it installs now.
- **Stored data and secret values stay out of it.** What the app stores stays
  under `data/.apps/<name>/` (through `DATA_DIR`) and secret values under
  `data/.secrets/`; never put either in the app directory.

A skill name, a routine id and an environment unit's file name are each unique
across the workspace. `uv run --no-sync app-install check` reports a clash.

Something elsewhere in the workspace that uses the app without belonging to it
(another creation's skill, a script, a doc) is one of the app's references,
each with a `note` naming the surface it uses. That is how it travels with the
app through hardening and testing.

- **A built-in app** registers them in its manifest, under `[[references]]`.
- **An app with a record** keeps them in the record, and the manifest check
  refuses `[[references]]` beside a record. Do not write the record by hand
  either:
  `uv run --no-sync app-install reference add <name> <path> --note "<the surface it uses>"`
  records that a path uses the app, and
  `uv run --no-sync app-install reference remove <name> <path>` takes the
  entry out again. Each commits that one file.

## A pinned window (`[pin]`)

A `[pin]` table (`path`, and optionally `style = "plain" | "avatar"`,
`scope = "linked" | "independent"`, `default_mode = "bar" | "floating"`) gives
the app one window at that path on every desktop that is never closed, and
whose taskbar entry each client may draw in the bar or floating above the
windows. Almost no app wants one (the chat's root window is the case it exists
for), so leave it out unless the user asked for an always-present window.

## Renaming an app

An app's `display_name` names more than its windows: every window's "Chat about
this app" button opens the chat titled `<display_name> Chat` (the multiplayer
spec, section 9). The button finds that chat again by the `app_chat` label it
carries with the app's registered name, so a renamed app keeps its chat. Its
title is what no longer matches, so rename the chat too.
`system/scripts/rename_chat.py` does it through the chat app's rename route
(`POST /api/chats/<id>/rename`, beside the send route `message_chat.py` posts
to):

```bash
python3 system/scripts/rename_chat.py --title "<old display name> Chat" --new-title "<new display name> Chat"
```

It exits 0 when the chat was renamed or there is no chat under the old title
(nothing to do), and non-zero when the chat app is unreachable or refused.
