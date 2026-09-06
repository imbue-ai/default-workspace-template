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
via `system/scripts/forward_port.py --manifest` -- from its program line, or
from inside its entry point once its socket is bound (the terminal, files and
chat apps do the latter) -- and then runs the
app.

Every Python app with a manifest runs from its own uv tool environment,
installed from its own `pyproject.toml` (`uv tool install -e
system/apps/<package>`, done by `system/scripts/build_workspace.sh` at image
build, by the build-app scaffold for a new app, and by the update-self apply
when an app's directory changes), so its program line runs the tool's entry
point rather than `uv run`. The root venv is for the background services,
agents, skills, and scripts. The manifest is the discriminator: an app
scaffolded before manifests existed has no `app.toml`, still runs `uv run
<name>` from the root venv, and keeps working that way for as long as it
exists (both forms are supported; nothing rewrites an old app behind the
user's back). Python packages here are picked up by the workspace's
`system/apps/*` uv member glob, so one lockfile covers the whole tree and
nothing in the root `pyproject.toml` needs editing for a new app; an app's own
tool environment is what keeps it running while the root venv is rewritten.

An app that needs a continuously running background component keeps that
service's code in its own folder here, named `<app>-<role>` in supervisord;
standalone background services live in `system/services/` instead.
