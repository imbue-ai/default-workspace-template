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
  persistent sessions.
- `browser/` - The live browser tab: a headless Chromium streamed to the UI.

Python packages in this folder are picked up automatically by the workspace's
`system/apps/*` uv member glob -- no central registration needed beyond the
root `pyproject.toml` dependency the scaffolder adds.

An app usually runs as a supervised service (a `[program:*]` entry in
`system/supervisord.conf`) and registers its port via
`system/scripts/forward_port.py`. An app that needs a continuously running
background component keeps that service's code in its own folder here, named
`<app>-<role>` in supervisord; standalone background services live in
`system/services/` instead.
