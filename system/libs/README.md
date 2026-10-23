# system/libs/

Support libraries: workspace packages that are neither apps (no window of
their own) nor background services (no supervised program of their own). Each
is a uv workspace member with its own `pyproject.toml`; see each package's
README for details.

- `app_manifest/` - The app manifest (`system/apps/<package>/app.toml`) and
  app registry (`data/.state/apps.toml`) models every app is described by,
  and the `app-manifest validate-manifest` command.
- `app_schema/` - An app's self-description: the OpenAPI document of its HTTP
  API, built from its Flask routes and served at the path its manifest names
  in `api_schema_path`, and the assertion an app's tests make about it.
- `workspace_layout/` - The shell's layout routes as one typed client the
  apps share, and the agent-facing `uv run --no-sync workspace-layout` command that
  reads and arranges the desktop.
- `workspace_ui/` - The frontends' shared JavaScript library (source only,
  a member of the npm workspace at `system/package.json`): the design
  system's token layer, the shared components, and the browser-side app
  contract and embed modules the frontends build from.
- `routines/` - Everything that runs on a schedule: the routine definitions
  under `system/routines/`, the scheduler, the runner for command runs and
  skill runs (automations), the run ledger, the cron env wrapper, and the
  `routines` command (see the manage-routines skill and the Routines app).
- `bootstrap/` - First-boot setup; then launches supervisord, which supervises
  the apps and services.
- `chat_background_tasks/` - The marker files recording the background tasks
  each chat waits on, and their readers.
- `github_sync/` - The opt-in GitHub auto-push wiring (a git hook, not a
  daemon; see the github-sync skill).
- `mngr_cli_contract/` - Shared validator that checks mngr CLI argvs against
  the live mngr command tree.
- `pytest_executable_tmp/` - Test-only pytest plugin that keeps a session's
  temporary files where a stub executable written there can run, or stops
  the session before any test starts.
- `pytest_workspace_env/` - Test-only pytest plugin that clears the
  environment an mngr agent runs with before any test code runs, and, with
  `DWT_REQUIRE_WORKSPACE_ENV=1` set, fails rather than skips any test (the CI
  job that runs the suites inside the workspace image sets it).
- `tk_command_parsing/` - Parsing helpers for the vendored `tk` ticket
  tracker's command output.

Apps live in `system/apps/`; standalone background services live in
`system/services/`.
