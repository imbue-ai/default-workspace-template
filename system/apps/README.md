# system/apps/

Apps: everything you can open as a tab in the workspace. Each app is a folder
here -- the built-in ones ship with the template, and apps your mind builds for
you land here too (see the build-app skill). The top-level `apps` symlink
points at this folder.

Built-in apps:

- `system_interface/` - The special one: the workspace UI itself. It hosts the
  tabs the other apps render in, so it is an app that also serves as the
  workspace chrome. Do not use it as a template for new apps.
- `chat/` - The chat app: the agent harness UI, one page per chat, rendered
  inside a tab's iframe at its own origin. The `chat` package (`chat-app`)
  runs `mngr observe` over the workspace's agents, serves the chat pages,
  their API, and the instances API on port 8010, and owns the provider
  accounts. Its frontend and the shell's are two builds of one npm workspace
  (`system/package.json`) sharing the `system/libs/workspace_ui` library.
- `terminal/` - The terminal tab (ttyd over the web), including its named
  persistent sessions; a Python package (`terminal-app`) that runs ttyd and
  serves the instances API over the workspace's tmux sessions.
- `files/` - The file viewer tab: the `files-app` package, the instances
  library's sidecar around dufs over `data/`.
- `browser/` - The live browser tab: a fleet of Chromium browsers streamed to
  the UI, whose daemon (`browser-service`) also serves the instances API over
  the fleet.

Every app describes itself in an `app.toml` manifest beside its code: its
registered name, the display name users see, its icon, whether it serves
instances, its memory-shedding `priority`, whether it is `critical`, and the
supervisord `program` that runs it (the schema is the `app_manifest` library
in `system/libs/`). An app runs as a supervised program (a `[program:*]` entry
in `system/supervisord.conf`) that registers the manifest and the app's port
via `system/scripts/forward_port.py --manifest`, from its program line or
from inside its entry point once its socket is bound, and then runs the app.

A manifest may also declare what belongs to the app *outside* its own folder.
`[[references]]` lists literal repo-root-relative files and directories the app
owns -- a skill that drives it, a script that launches it, a doc that describes
it -- each with an optional one-line `note` saying which of the app's surfaces
it uses; `[scope] exclude` lists gitignore-style globs that no review or test
pass considers, on top of the built-in ones (`system/vendor/**`, `data/**`,
`**/node_modules/**`, `**/static/**`, `**/.venv/**`). A reference may not point
inside the app's own directory (already implicit) or into another app's (that is
a `pyproject.toml` dependency), and every one must exist, which
`system/test_app_manifests.py` checks for every manifest in the tree. Together
with the app's directory and its `system/supervisord.conf` blocks these make up
the app's footprint, which `app-manifest footprint <manifest>` writes out as a
scope file; `app-manifest references --for-path <path>` runs the lookup the
other way, from an owned path back to the app that claims it. Registration
ignores both tables entirely, so a stale reference can never stop the app.

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
