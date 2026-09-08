# system/libs/

Support libraries: workspace packages that are neither apps (no tab) nor
background services (no supervised program of their own). Each is a uv
workspace member with its own `pyproject.toml`; see each package's README for
details.

- `app_manifest/` - The app manifest (`system/apps/<package>/app.toml`) and
  app registry (`data/.state/apps.toml`) models every app is described by,
  and the `app-manifest validate-manifest` command.
- `app_instances/` - The instances API every multi-instance app serves: the
  Flask blueprint over a pluggable instance source, the JSON store, the nudge to
  the shell, and the sidecar launcher that wraps a third-party server.
- `workspace_ui/` - The frontends' shared JavaScript library (source only,
  a member of the npm workspace at `system/package.json`): the design
  system's token layer, the shared components, the address and view helpers,
  and the browser-side app contract and embed modules the shell and the chat
  page both build from.
- `automations/` - The machinery that runs automations (skills on a
  schedule): the durable recurring-job runner, the cron env wrapper, and the
  automation-agent waker (see the manage-scheduled-tasks skill).
- `bootstrap/` - First-boot setup; then launches supervisord, which supervises
  the apps and services.
- `detached_subprocess/` - The way a workspace service shells out: three entry
  points that each run a child in its own session, out of reach of the
  workspace's terminal, plus the two ratchet rules every supervisord program
  applies to its own source tree to stay on them.
- `github_sync/` - The opt-in GitHub auto-push wiring (a git hook, not a
  daemon; see the github-sync skill).
- `mngr_cli_contract/` - Shared validator that checks mngr CLI argvs against
  the live mngr command tree.
- `tk_command_parsing/` - Parsing helpers for the vendored `tk` ticket
  tracker's command output.

Apps live in `system/apps/`; standalone background services live in
`system/services/`.
