# system/apps/

Apps: everything you can open as a window on the workspace's desktop. Each app is a folder
here -- the built-in ones ship with the template, and apps your agent builds for
you land here too (see the build-app skill). The top-level `apps` symlink
points at this folder.

Built-in apps:

- `system_interface/` - The special one: the workspace UI itself, the desktop
  the other apps' pages render in as windows, so it is an app that also serves
  as the workspace chrome. Do not use it as a template for new apps.
- `chat/` - The chat app: the agent harness UI, one page per chat, rendered
  inside a window's iframe at its own origin. The `chat` package (`chat-app`)
  follows the workspace's agent observer (the `agent-observer` program, one
  `mngr observe` per workspace), serves the chat pages and their API on port
  8010, and owns the provider accounts. Its frontend, the shell's, the
  Getting Started app's and Files' are builds of one npm workspace (`system/package.json`)
  sharing the `system/libs/workspace_ui` library.
- `getting_started/` - The Getting Started page: the "Start something" intents
  and the "Start from a template" shelves (the published template catalog it
  fetches from `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL`, see `catalog/README.md`),
  each starting a chat with a seeded text through the shell's
  `shell:start-with-text`. It opens its own window once per workspace, on the
  first desktop for the first client that connects, and remembers having done
  so under `data/.state/getting-started/`. Served on port 8030 by the
  `getting-started` package.
- `terminal/` - The terminal (ttyd over the web), including its named
  persistent sessions; a Python package with two entry points: `terminal-app`
  serves the wrapper pages (each frames one session's ttyd page) over the
  workspace's tmux sessions, and `terminal-pty` runs ttyd itself on its own
  internal origin.
- `terminal_pty/` - Only the manifest of that ttyd origin (`terminal-pty`,
  internal); the program that registers it is the `terminal-pty` entry point of
  `terminal/`.
- `files/` - Files: a Finder-like browser of the container's filesystem, opened
  at the workspace folder. Its `files-app` entry point runs a small server that
  serves the files, and the files shared from the user's computers, with a
  frontend from that same npm workspace.
- `browser/` - The live browser: a fleet of Chromium browsers streamed to the
  UI by its daemon (`browser-service`).
- `routines/` - Routines: everything that runs on a schedule (the routines
  library, `system/libs/routines/`), each with its health, next run and history,
  and run-now, on/off, reschedule and fix controls. Served on port 8050 by the
  `routines-app` package; it stores nothing of its own.
- `system_monitor/` - System Monitor: how much CPU, memory and disk the
  workspace is using, on an overview and a view per resource: which chats,
  code checkers, and apps use them, the busy stretches, which programs the
  memory guard stopped in the last 24 hours, and the state of the backups.
  Served on port 8040 by the `system-monitor` package; it keeps nothing on
  disk (its graphs come from the `resource_sampler` program beside the memory
  guard, `system/services/oom_priority/`).
- `datalib/` - The Datalib app: datalib's web UI (`datalib-http`) over the store
  the `datalib` skill writes. Not announced to users yet: its program is not in
  `system/supervisord.conf.d/` until that skill's install script copies it there,
  so the app is not listed until then.

Every app describes itself in an `app.toml` manifest beside its code: its
registered name, the display name users see, its icon, the launch paths the
desktop opens windows at, its memory-shedding `priority`, whether it is `critical`, whether it
stops once no window shows it (`stop_when_no_windows`), and the
supervisord `program` that runs it (the schema is the `app_manifest` library
in `system/libs/`). An app runs as a supervised program (a `[program:*]` entry
in its own `system/supervisord.conf.d/<name>.conf`) that registers the manifest
and the app's port via `system/scripts/forward_port.py --manifest`, from its
program line or from inside its entry point once its socket is bound, and then
runs the app.

A manifest may also declare what belongs to the app *outside* its own folder.
`[[references]]` lists literal repo-root-relative files and directories the app
owns -- a skill that drives it, a script that launches it, a doc that describes
it -- each with an optional one-line `note` saying which of the app's surfaces
it uses; `[scope] exclude` lists gitignore-style globs that no review or test
pass considers, on top of the built-in ones (`system/vendor/**`, `data/**`,
`**/node_modules/**`, `**/dist/**`, `**/.venv/**`); `[wiring] programs` names
the supervisord programs the app owns beyond its own block and its
`<name>-<role>` sidecars (the browser declares `xvfb`). A reference may not point
inside the app's own directory (already implicit) or into another app's (that is
a `pyproject.toml` dependency), and every one must exist, which
`system/test_app_manifests.py` checks for every manifest in the tree. Together
with the app's directory and the supervisord blocks that run it, these make up
the app's footprint, which `app-manifest footprint <manifest>` writes out as a
scope file; `app-manifest references --for-path <path>` runs the lookup the
other way, from an owned path back to the app that claims it. Registration
ignores both tables, so a stale reference can never stop the app.

Every Python app with a manifest runs from its own uv tool environment,
installed from its own `pyproject.toml` (`uv tool install -e
system/apps/<package>`, done by `system/scripts/build_workspace.sh` at image
build, by the build-app scaffold for a new app, and by the update-self apply
when an app's directory changes), so its program line runs the tool's entry
point rather than `uv run`. The root venv is for the background services,
agents, skills, and scripts. The manifest is the discriminator: an app with
no `app.toml` runs `uv run <name>` from the root venv, and both forms are
supported (nothing converts an app from one form to the other without the
user). Python packages here are picked up by the workspace's `system/apps/*`
uv member glob, so one lockfile covers the whole tree and nothing in the root
`pyproject.toml` needs editing for a new app; an app's own tool environment is
what keeps it running while the root venv is rewritten.

An app that needs a continuously running background component keeps that
service's code in its own folder here, named `<app>-<role>` in supervisord;
standalone background services live in `system/services/` instead.
