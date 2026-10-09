---
name: build-app
description: "Use when you want to create a new app for the user -- a page, dashboard, or tool they can open as a window on the desktop. Covers scaffolding a new Flask app (canonical path), registering it so it is served on its own port, and the escape hatch for wrapping a pre-existing third-party server."
metadata:
  author: imbue
  crystallized: true
---

# How to build an app

An "app" here is something the user can open as a window in
the desktop client and see render at its own browser origin --
locally `http://<name>.<workspace-host>/` (e.g.
`http://news.host-ab12.localhost:8421/`). The forwarder routes that
origin straight to the port you register; nothing proxies or rewrites
your app's traffic.

There is one canonical path (scaffold a new Flask lib) and one
escape hatch (wrap a pre-existing third-party server). Modify/remove
flows go through the `update-app` skill.

## Before you start

Ask only the questions that genuinely *block* -- a fork that is both genuinely
uncertain *and* expensive to reverse later. Most apps have none: default to
the simplest conventional choice and to a **single user**, and build.

## Decide which path applies

- **Authoring routes yourself** (the common case): use the Flask
  scaffolder in Step 1. The scaffolder picks correct defaults so most
  framework gotchas don't fire.
- **Wrapping a pre-existing third-party server** (Jupyter, Grafana,
  an `npx`-installed dashboard, anything with its own start command):
  skip the scaffolder, jump to "Escape hatch: wrap an existing server"
  below.

If you would otherwise scaffold a Flask lib whose only job is to
shell out to a third-party tool, do not do that -- the forwarder
already routes the service's origin to whatever URL you register.
Adding a Python proxy in front of the third-party server adds a hop,
costs an extra process, and complicates WebSocket and streaming
behavior. Use the escape hatch instead.

Do not extend `system/apps/system_interface/` to add a new view. That app runs
the top-level workspace UI; new apps go in their own scaffolded lib
under `system/apps/<your-package>/` so they get an isolated window and origin.

## Pre-flight (both paths)

- **Pick a kebab-case app name.** Becomes the service's hostname
  label: the window renders at `http://<name>.<workspace-host>/`, so the
  name must be DNS-safe -- lowercase letters/digits with single
  hyphens, and it must not start with `host-` or `agent-` (those
  prefixes are reserved for workspace hostname coordinates), and it must not
  be the first label of a standalone service (`share`, `app`, `owner`, `vm`,
  `host`, `env`, `agent`, and `github` for the `github-sync` program enabling
  GitHub sync adds), which would claim that service as a sidecar. Short and
  descriptive (`news`, `docs-viewer`) beats clever. Avoid names
  already used by an existing program (`system_interface`, `browser`, etc.
  are reserved by the scaffolder, which also refuses a name any
  `system/supervisord.conf.d/*.conf` already declares).
- **Draw the app's icon** -- a 216 by 216 two-layer tile specific to what
  *this* app does, built to the rules in `docs/system/app-icons.md` (a
  flat background under one fill-only glyph inside a centred 144 box, in
  a colour pair from the palette that doc carries);
  `forward_port.py` refuses a brand-new registration without one. The
  scaffold copies it beside the app's manifest (`app.toml`), which names
  it.
- **Pick a free port.** The scaffolder (canonical path) auto-picks the lowest free
  port at or above 8080 by parsing `system/supervisord.conf`, every
  `system/supervisord.conf.d/*.conf`, and `data/.state/apps.toml`, so running
  manual port checks (`ss -tln`) is unnecessary. If you are picking a port
  manually for the wrap-existing escape hatch, check `ss -tln` and avoid `8000`
  (system_interface), `8010` (the chat app), `8030` (the Getting Started app) and
  `8081` (the browser service).
  Two things do not show up there: the `agent-observer` program binds no port at
  all, and a preview of any app (`update-app`'s `preview_app.py`) takes free
  ports at boot, so nothing to avoid is written down for it.
- **Bind to `127.0.0.1`** (not `0.0.0.0`). The forwarder reaches your
  app from inside the same container; binding to all interfaces is
  noise. The scaffolder does this. For the wrap-existing path, many
  Node frameworks default to `0.0.0.0` -- pass an explicit host
  (`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) if your
  third-party tool's default isn't loopback. Python defaults are
  usually loopback already.

## Step 1: Run the scaffolder (canonical path)

```bash
uv run .agents/skills/build-app/scripts/scaffold_flask_lib.py \
    --name <service-name> \
    --description "<one-liner>" \
    --icon-file <path-to-svg> \
    --start \
    [--display-name "<what users see>"] \
    [--port <int>] \
    [--extra-dep <pkg>] [--extra-dep <pkg>] ...
```

Required:
- `--name`: kebab-case (lowercase letters/digits with single hyphens;
  must not start with `host-` or `agent-`) -- it becomes the service's
  hostname label.
- `--description`: becomes the lib `pyproject.toml` description.
- `--icon-file`: the icon you drew in pre-flight (`.svg` only); copied
  to `system/apps/<package>/icon.svg`, named by the manifest, and
  registered on every start.

Optional:
- `--start`: registers the app with supervisord (`reread` + `update`) and
  waits for the service to answer healthy on `http://127.0.0.1:<port>/health`.
  Recommended to eliminate separate manual supervisor commands.
- `--display-name`: what users see for the app (the manifest's
  `display_name`, at most 64 characters). Defaults to the description,
  so pass it when the description is long.
- `--port`: explicit port; auto-picked if omitted.
- `--extra-dep`: repeatable. Add libraries beyond `flask`/`flask-sock`
  (e.g. `--extra-dep "jinja2>=3.1" --extra-dep "anthropic>=0.40"`).
- `--skip-uv-sync`: skip the final manifest check, tool install and
  `uv sync --all-packages` (for fast iteration / dry runs).

The scaffolder fails non-zero with a clear stderr message if the lib
already exists, the name is reserved or invalid, the requested port
is taken, or the manifest check, the tool install or `uv sync` fails.

What gets generated:

- `system/apps/<package>/app.toml` -- the app's manifest: its registered
  `name`, `display_name`, `icon`,
  `priority = "user"` (shed before any built-in under memory pressure),
  `program` (its supervisord program), and `stop_when_no_windows = true` (the
  shell stops the app a minute after its last window closes and starts it
  again on the next request; `false` keeps it running for the life of the
  workspace). Leave it `true` for an app that only answers requests: the stop
  frees the whole process while nobody looks at it, its data on disk is
  untouched, and the next request wakes it. Set it to `false` when the app
  does work between requests that a stop would lose or interrupt: a
  background thread or scheduler that refreshes data, a poller or file
  watcher, a websocket or subscription to an outside service, a job that
  must finish after the user closes the window, or an API another agent
  drives with no window open. A stopped app runs nothing until its next
  request, and the wake serves that request, not the work that was in
  flight. `forward_port.py --manifest`
  reads it on every start; the scaffold checks it with `uv run app-manifest
  validate-manifest system/apps/<package>/app.toml` (run that yourself after
  editing it). Anything you build for this app outside `system/apps/<package>/`
  -- a skill that drives it, a script, a doc -- is registered in the same file
  under `[[references]]` with a `note` naming the surface it uses.
- `system/apps/<package>/pyproject.toml` -- declares
  `[project.scripts] <name> = "<package>.runner:main"`, the entry point
  the app's own tool environment exposes, and makes the directory its own
  `ty` project (an empty `[tool.ty]` table and a pinned `ty` dev dependency)
  for the ratchets' type check.
- `system/apps/<package>/src/<package>/__init__.py` -- empty.
- `system/apps/<package>/src/<package>/runner.py` -- sync Flask starter.
  Builds a `Flask` app and serves it with
  `werkzeug.serving.run_simple(..., threaded=True)`. It serves at `/`,
  and the app owns its own browser origin, so no path prefix or
  `root_path`/`ROOT_PATH` is needed. It also defines a `DATA_DIR`
  constant (defaults to `data/.apps/<name>/`, overridable via the
  `<PACKAGE_UPPER>_DATA_DIR` env var) -- route all persistent state
  through it (see File-path conventions below) -- and a `PORT` constant
  (defaults to this service's assigned port, overridable via the
  `<PACKAGE_UPPER>_PORT` env var) bound in `run_simple`. Both overrides
  are what let a future edit boot a throwaway instance on a spare port
  against a data copy (see `update-app`). The scaffolded index page also
  carries the **shell page script** (`SHELL_PAGE_SCRIPT` in the runner) -- a
  module script that connects the page to the workspace shell framing it,
  reports where the page is on the handshake (so the shell reopens this
  app's window at the place it was showing), and installs the **element
  context menu**: the right-click menu whose last rows ("Copy reference",
  "Explain...", "Modify...") hand the clicked element to a chat as a
  `REF-<id>.json` attachment an agent can resolve
  (`.agents/shared/references/element-references.md`,
  `docs/system/blueprint/element-reference-menu/`). Keep the script on
  every page the app serves. An app that drops it always reopens at its
  origin and gets the browser's own menu. The runner serves the two modules
  the script imports from its own origin at `/_static/app_contract.js` and
  `/_static/context_menu.js` (a module import is a fetch without cookies,
  which the forwarder refuses across origins); keep that route too.
  A reference names an element by what its markup carries -- its `id`, its
  `data-*` attributes, its classes, a selector -- so give a list
  row the `id` or a `data-*` attribute of the record it shows, and an
  interactive control a stable `id` or a first class that names it, and a
  reference resolves to one thing.
- `system/apps/<package>/test_<package>_ratchets.py` -- standard ratchets at
  zero, plus `test_no_type_errors`, which fails on any `ty` error in the app.
- `system/apps/<package>/README.md` -- one-line description.

What gets updated and installed -- no shared file is authored, which is what
lets two agents scaffold two apps at once (`uv.lock` is the exception: `uv sync`
regenerates it, but it is derived, so it stays out of a creation's footprint):

- Root `pyproject.toml` -- untouched. The `system/apps/*` member glob picks the
  package up and `uv sync --all-packages` installs it, so a scaffolded app
  needs no root entry at all.
- `system/supervisord.conf.d/<name>.conf` -- writes the app's own program block:

  ```ini
  [program:<name>]
  command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url http://localhost:<port> && exec <name>"
  directory=/home/user/workspace
  autostart=true
  autorestart=true
  startsecs=30
  startretries=5
  # plus rotated stdout/stderr logfiles under /var/log/supervisor/<name>-*.log
  ```

  `startsecs`/`startretries` bound a crash loop: an app that dies before it has
  stayed up 30s counts as a failed start, so supervisord backs off and ends in
  FATAL rather than restarting a broken app several times a second for the life
  of the workspace. Built-in services deliberately retry forever instead.

  The command ends in `exec` of the app's own name, not `uv run <name>`, so
  the app is the process supervisord tagged rather than a child of a wrapper;
  supervisord resolves that name on PATH. The copy it finds is the console script
  `uv sync --all-packages` writes into the workspace venv -- `uv tool install
  -e` puts the tool's own entry point under your HOME, which supervisord's
  children do not have on PATH. So always sync with `--all-packages`: a
  root-closure-scoped `uv sync` prunes the member (a scaffolded app is not a
  root dependency), deletes that script, and the next restart is a spawn error
  with nothing to recover it.

  An app that needs a credential (an API key the user supplied through the
  `connect-external-service` skill's secret card) declares it in `app.toml` and
  runs under the wrapper. `--secrets-file <name>` makes the scaffold write the
  wrapped program command; the `[[secrets]]` block you add to the manifest by
  hand either way, since only you know the variables. The block names the file,
  its variables, and a one-line note (`publish-template` reads it, and refuses
  to assemble a program that runs under a file no block declares), and the
  program command wraps the entry point so the file's variables reach the
  process and nothing else does:

  ```toml
  [[secrets]]
  file = "example"
  variables = ["EXAMPLE_API_KEY"]
  note = "An Example API key from the account's settings page"
  ```

  ```ini
  command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url http://localhost:<port> && exec python3 system/scripts/with_secrets.py data/.secrets/example.env -- <name>"
  ```

  A preview (`update-app`'s `preview_app.py`) runs the app under the same
  wrapper, once per `[[secrets]]` block, reading the live `data/.secrets/`.

  The Flask app serves at `/` and needs no prefix env var: your app
  owns its origin, so root-absolute URLs (`href="/api"`), WebSockets
  (`new WebSocket("/ws")`), cookies (`Set-Cookie: Path=/`), and
  service workers all work exactly as written -- nothing rewrites
  anything. The
  `bash -c "..."` wrapper is required because supervisord runs commands
  directly (no shell) and this one chains `forward_port.py` with `&&`. The
  `oom_tag_service.py user` prefix tags this user-created app so it is
  shed before any built-in service under memory pressure (see
  `system/services/oom_priority/README.md`).
- The app's own uv tool environment: the scaffold runs
  `uv tool install -e system/apps/<package>`, which is what puts the
  `<name>` entry point the program line runs on PATH. Every Python app
  runs from its own tool rather than the root venv (the root venv is for
  background services, agents, skills, and scripts), so a dependency you
  add later needs `uv tool install -e system/apps/<package> --reinstall`
  (see `update-app`). The root `pyproject.toml` is not edited: the
  `system/apps/*` member glob already covers the package, and the final
  `uv sync --all-packages` keeps the root lockfile current for it.

If you passed `--start` to `scaffold_flask_lib.py`, the service is already registered and running healthy.
If you ran without `--start`, tell supervisord to pick up the new program:

```bash
supervisorctl reread && supervisorctl update
supervisorctl status <name>
```

`update` does not wait for the start, so a healthy new app reads `STARTING`
for its first 30 seconds -- that is the `startsecs` window the program block
sets, not a failure. `BACKOFF` or `FATAL` is the failure signal, and a broken
app reaches it well before the window is up. On either, read its log
(`/var/log/supervisor/<name>-stderr.log`) or run
`supervisorctl tail <name> stderr`.

### Design

Read `references/frontend-choices.md` for recommended design choices.

## Step 2: Build the routes

The starter `runner.py` has just `GET /` (a placeholder HTML page)
and `GET /health` (returns `{"status": "ok"}`). Replace the
placeholder with your real routes.

Use **sync handlers** (`def`, not `async def`). Flask handlers are
sync `def`, and the starter runs on the threaded Werkzeug server
(`run_simple(..., threaded=True)`), so concurrent requests are handled
by separate threads -- no asyncio needed.

### Calling Claude from your service

If your service needs to call Claude (classify/summarize content, run a one-shot
agentic task, or launch a full agent), follow the `use-ai-integration` skill: it
picks the path (a keyed `litellm` call or the keyless `claude_p.py` helper),
covers the `claude -p` environment fix and the cost model, and saves you from
hand-rolling the call.

### Always surface the raw data and its source

When a view renders data *derived* from underlying records (a summary,
a reformatted list, extracted fields), include -- by default, without
the user asking -- a "view raw" control showing the original record
**rendered in its native format** (an HTML email as the rendered email,
not escaped source; JSON pretty-printed; markdown rendered -- the
faithful original minus your processing) plus, for records from an
external service, an "open in <source>" link back to the origin (e.g.
open the email in Gmail). When you render untrusted third-party HTML (a
raw email body is the common case), sandbox it -- a sandboxed `iframe`
or a sanitizer -- so the view can't run scripts or phone home via
tracking pixels.

This is the surfacing half of the preserve-and-surface principle
(CLAUDE.md): the derived view inevitably leaves gaps (a field the agent
didn't extract, a rendering it didn't anticipate), and the raw/source
affordance lets the user bridge them without waiting for a rebuild.
Design it in from the first version -- it depends on the data layer
having persisted the raw payload and source reference, so confirm
that's available and flag it if it isn't. Keep it unobtrusive (a small per-record control,
not clutter) and don't call it out in chat -- always present, never
announced.

### File-path conventions

Two cases, two patterns:

- **Persistent state** (caches, cursors, last-visit timestamps, JSON
  snapshots, user records -- anything written and read across runs):
  read and write it under the generated `DATA_DIR` constant, never a
  hardcoded `data/.apps/<name>/` at the call site. `DATA_DIR` defaults to
  `data/.apps/<name>/` (cwd-relative, resolved from `/home/user/workspace` where the
  supervisord-managed service runs) but honors the
  `<PACKAGE_UPPER>_DATA_DIR` env var. That override is what makes a
  future edit safe: an agent changing the service can run a throwaway
  instance against a *copy* of the data instead of the live store (see
  `update-app`), so keep every read/write going through `DATA_DIR`
  -- a hardcoded `data/.apps/<name>/` silently bypasses the override and
  re-exposes the live data. Do NOT use `Path(__file__)`-based paths for
  state. A store the app can rebuild (downloads, extracted archives,
  clones, caches) gets its own directory under `DATA_DIR` with an
  empty `.nobackup` file in it, so the hourly backup skips it.
- **Static assets shipped alongside the .py file** (templates,
  default configs, bundled JSON): `Path(__file__).parent / "assets/..."`
  is the right pattern.

## Step 3: Checking it (optional)

Checking the app before replying is optional. If you want to,
`python3 system/scripts/smoketest_app.py <name> --marker "<expected text>"`
probes it ([references/verify.md](references/verify.md)), and
[references/cross-flow-gotchas.md](references/cross-flow-gotchas.md) is
symptom-indexed for connection refused, a window stuck on the loading
page, or broken WebSockets.

## Escape hatch: wrap an existing server

For pre-existing third-party tools, do not scaffold a lib. Save your
icon as `system/apps/<name>/icon.svg` and write the app's manifest beside
it as `system/apps/<name>/app.toml` (like the `files` app):

```toml
name = "<name>"
display_name = "<What users see>"
icon = "icon.svg"
priority = "user"
program = "<name>"
```

Then add a `[program:<name>]` block as its own
`system/supervisord.conf.d/<name>.conf` that runs `forward_port.py --manifest`
and then your existing start command. supervisord runs commands directly (no
shell), so wrap any command that chains with `&&` in `bash -c "..."`, and
prefix the whole thing with
`python3 system/services/oom_priority/bin/oom_tag_service.py user` so this user-created app is
shed before any built-in service under memory pressure (see
`system/services/oom_priority/README.md`):

```ini
[program:<name>]
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --manifest system/apps/<name>/app.toml --url http://localhost:<port> && exec <existing_start_command>"
directory=/home/user/workspace
autostart=true
autorestart=true
startsecs=30
startretries=5
```

Two valid shapes:

- **Inline** (preferred when one line fits):

  ```ini
  [program:docs-viewer]
  command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --manifest system/apps/docs-viewer/app.toml --url http://localhost:8090 && exec jupyter notebook --port 8090 --ip 127.0.0.1 --no-browser"
  directory=/home/user/workspace
  autostart=true
  autorestart=true
  startsecs=30
  startretries=5
  ```

- **Wrapper script** (preferred for multi-step bootstrap or env exports):

  ```bash
  # system/scripts/run_<name>.sh
  #!/usr/bin/env bash
  set -euo pipefail
  python3 system/scripts/forward_port.py --manifest system/apps/<name>/app.toml --url http://localhost:<port>
  exec <existing_start_command>
  ```

  ```ini
  [program:<name>]
  command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash system/scripts/run_<name>.sh
  directory=/home/user/workspace
  autostart=true
  autorestart=true
  startsecs=30
  startretries=5
  ```

After writing `system/supervisord.conf.d/<name>.conf`, run `supervisorctl
reread && supervisorctl update` to start the new program.

The `forward_port.py` call MUST come first in the command -- the port
must be registered before the app starts listening, otherwise the
shell's announcement of the registration races with the backend coming up.

For the full program schema and logging knobs, see the shared
[`.agents/shared/references/service-processes.md`](../../shared/references/service-processes.md).

The gotchas reference applies identically to this path.

## `forward_port.py` CLI reference

Used by both paths (the scaffolder generates the call; the escape
hatch has you write it directly).

```
python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url URL
python3 system/scripts/forward_port.py --name NAME --url URL --icon-file PATH
python3 system/scripts/forward_port.py --name NAME --remove
```

The script is standard-library only and runs under a plain `python3`, so
registration never depends on the root venv.

Flags:

- `--manifest`: the app's `app.toml`. Its `name` (validated like
  `--name` below), the icon file it names (validated like `--icon-file`),
  and its static fields (`display_name`, `critical`, `priority`, `program`,
  `internal`, `launcher_rank`, `default_shortcut`, `launch_paths`, `pin`) are
  copied onto the registry row on every
  call, so a changed manifest updates the row on the next start. This is
  the form every app with a directory uses. A `[pin]` table (`path`, and
  optionally `style = "plain" | "avatar"`, `scope = "linked" | "independent"`,
  `default_mode = "bar" | "floating"`) gives the app one window at that path
  on every desktop that is never closed and whose taskbar entry each client
  may draw in the bar or floating above the windows; almost no app wants
  one (the chat's root window is the case it exists for), so leave it out
  unless the user asked for an always-present window. `--name` may accompany it and
  must then equal the manifest's name; `--icon-file`, `--program`,
  `--internal` and `--no-icon` are for registrations with no app directory
  (previews, isolated test servers) and cannot be combined with it.
- `--name`: app name. It becomes the service's hostname label (the
  window renders at `http://<name>.<workspace-host>/`), so it is
  validated: lowercase letters/digits/underscores with single hyphens,
  and it must not be `localhost` or start with `host-` or `agent-`
  (reserved for workspace hostname coordinates). Registration fails
  loudly on an invalid name.
- `--url`: full URL where the app is reachable from inside the
  container (e.g. `http://localhost:8090`).
- `--icon-file`: path to the app's `.svg` icon (SVG only -- no
  rasters), drawn instead of the generic letter monogram. **Required
  when creating a new entry** (unless `--internal` or `--no-icon`);
  omitting it on re-registration keeps the stored icon. The file's
  *contents* are stored: a single safe `<svg>` element (no script,
  style, event handlers, or external references; at most 16384
  characters). A bad file fails a new registration loudly, but only
  warns on re-registration (the stored icon is kept), so a corrupted
  icon cannot crash-loop a running app.

  **Draw the icon to the rules in `docs/system/app-icons.md`**: a 216 by
  216 tile of exactly two layers -- a flat background, and one glyph in a
  second colour that fits a 144 by 144 box centred in it. The tile is
  drawn small but authored large, so the hand-drawn detail the look asks
  for has a grid to sit on. The frame to author in is

  ```svg
  <svg xmlns="http://www.w3.org/2000/svg" width="216" height="216" viewBox="0 0 216 216" fill="none">
  <rect width="216" height="216" rx="69.12" fill="BACKGROUND"/>
  <path d="..." fill="FOREGROUND"/>
  </svg>
  ```

  -- fills only, never strokes; no shadow (every surface that draws an
  icon casts its own); the 32 percent corner radius the shell's
  `--desk-icon-radius` also uses; and a root `fill="none"`, so the shell
  does not ink the tile with `currentColor` the way it inks a line
  glyph. The colours are a pair from the palette in that doc, and the
  glyph is one iconic *object* for what the app is *for* -- an
  envelope, a clipboard, a bell -- drawn true to that object's own
  proportions and then inked: the whole mark about a degree off level,
  the width of every run breathing a fifth either side of its nominal,
  the line itself drifting only a unit or two, ends round and lifting,
  with its interior detail cut through to the background colour at one
  nominal weight the whole set shares. Drawn in one pass and never revised. That doc carries the
  full look, and says how an icon that already exists is changed
  without losing its hand-placed points.
- `--no-icon`: skip the icon requirement for a brand-new entry. Uses
  the generic letter monogram. Use this only when the user explicitly
  declines an icon, or for short-lived preview windows.
- `--program`: name of the supervisord program that runs the app --
  the program-name-equals-service-name convention both paths follow, so
  pass the app's own name. Its presence on the registry entry is what
  lets the shell act on the app's process over supervisord's RPC: Quit it
  from its window menu, stop it once no window shows it, hold its port
  while it is stopped, and start it again on the next request (the
  `/api/apps/<name>/stop` and `/start` routes remain for agents);
  omitting it clears any previously-stored value, so every registration
  call is authoritative. Never pass it for unsupervised instances
  (previews, `serve_isolated_instance.py` test servers) -- those own
  their own teardown and must not offer a Stop that supervisord cannot
  honor.
- `--remove`: remove the named entry from
  `data/.state/apps.toml`. Use this when tearing down a service.

## The shared (public) URL

If the workspace is shared, every registered service is also reachable at
its own public origin -- the same prefix rule on the share hostname
(`https://<name>.<workspace-share-host>/`) -- with caveats about where that
hostname lives and why it isn't in `data/.state/apps.toml`. See
[references/public-url.md](references/public-url.md).

## Cleanup

To remove an app (drop the `apps.toml` entry, stop and unregister
the supervisord program, and revert the scaffolded lib), see
[references/cleanup.md](references/cleanup.md).
