# system/apps/

Apps: everything you can open as a tab in the workspace. Each app is a folder
here -- the built-in ones ship with the template, and apps your mind builds for
you land here too (see the build-app skill). The top-level `apps` symlink
points at this folder.

Built-in apps:

- `system_interface/` - The special one: the workspace UI itself. It hosts the
  tabs the other apps render in, so it is an app that also serves as the
  workspace chrome. Do not use it as a template for new apps.
- `terminal/` - The terminal tab (ttyd over the web), including its named
  persistent sessions; a Python package (`terminal-app`) that runs ttyd and
  serves the instances API over the workspace's tmux sessions.
- `files/` - The file viewer tab (dufs over `data/`).
- `browser/` - The live browser tab: a headless Chromium streamed to the UI.

Every app describes itself in an `app.toml` manifest beside its code: its
registered name, the display name users see, its icon, whether it serves
instances, its memory-shedding `priority`, whether it is `critical`, and the
supervisord `program` that runs it (the schema is the `app_manifest` library
in `system/libs/`). An app runs as a supervised program (a `[program:*]` entry
in `system/supervisord.conf`) whose command registers the manifest and the
app's port via `system/scripts/forward_port.py --manifest` and then runs the
app.

Every Python app with a manifest runs from its own uv tool environment,
installed from its own `pyproject.toml` (`uv tool install -e
system/apps/<package>`, done by `system/scripts/build_workspace.sh` at image
build, by the build-app scaffold for a new app, and by the update-self apply
when an app's directory changes), so its program line runs the tool's entry
point rather than `uv run`. The root venv is for the background services,
agents, skills, and scripts. The manifest is the discriminator: an app
scaffolded before manifests existed has no `app.toml`, still runs `uv run
<name>` from the root venv, and is left as it is until the workspace app
model's migration rewrites it to the manifest form. Python packages here are
still picked up by the workspace's `system/apps/*` uv member glob for now, so
one lockfile covers the whole tree; nothing in the root `pyproject.toml` needs
editing for a new app.

An app that needs a continuously running background component keeps that
service's code in its own folder here, named `<app>-<role>` in supervisord;
standalone background services live in `system/services/` instead.
