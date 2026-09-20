# system/apps/

Apps: everything you can open as a tab in the workspace. Each app is a folder
here -- the built-in ones ship with the template, and apps your mind builds for
you land here too (see the build-app skill). The top-level `apps` symlink
points at this folder.

Built-in apps:

- `system_interface/` - The special one: the workspace UI itself, the desktop
  the other apps' pages render in as windows, so it is an app that also serves
  as the workspace chrome. Do not use it as a template for new apps.
- `chat/` - The chat app: the agent harness UI, one page per chat, rendered
  inside a window's iframe at its own origin. The `chat` package (`chat-app`)
  runs `mngr observe` over the workspace's agents, serves the chat pages and
  their API on port 8010, and owns the provider accounts. Its frontend and the
  shell's are two builds of one npm workspace (`system/package.json`) sharing
  the `system/libs/workspace_ui` library.
- `terminal/` - The terminal (ttyd over the web), including its named
  persistent sessions; a Python package with two entry points: `terminal-app`
  serves the wrapper pages (each frames one session's ttyd page) over the
  workspace's tmux sessions, and `terminal-pty` runs ttyd itself on its own
  internal origin.
- `terminal_pty/` - Only the manifest of that ttyd origin (`terminal-pty`,
  internal); the program that registers it is the `terminal-pty` entry point of
  `terminal/`.
- `files/` - The file viewer: dufs over `data/`, run from its program line with
  a vendored, patched frontend.
- `browser/` - The live browser: a fleet of Chromium browsers streamed to the
  UI by its daemon (`browser-service`).

Every app describes itself in an `app.toml` manifest beside its code: its
registered name, the display name users see, its icon, the launch paths the
desktop opens windows at, its memory-shedding `priority`, whether it is `critical`, and the
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
with the app's directory and its `system/supervisord.conf` blocks, these make up
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
