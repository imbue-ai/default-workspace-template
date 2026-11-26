# system/libs/

Support libraries: workspace packages that are neither apps (no window of
their own) nor background services (no supervised program of their own). Each
is a uv workspace member with its own `pyproject.toml`; see each package's
README for details.

- `app_manifest/` - The app manifest (`system/apps/<package>/app.toml`) and
  app registry (`data/.state/apps.toml`) models every app is described by,
  and the `app-manifest validate-manifest` command.
- `app_registration/` - Writes an app's row in the app registry: what
  `system/scripts/forward_port.py` runs, stdlib-only.
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
- `workspace_voice/` - Dictation for the frontends (source only, a member of
  the same npm workspace): on-device speech recognition behind one engine
  interface, the speech detector, the streaming turn, the inference lane, and
  the engine worker every chat page shares.
- `latchkey_access/` - The latchkey gateway as a workspace program reaches it: the
  environment and header names of the agent-to-gateway contract, and `GatewayAccess`,
  which reads the environment and builds a request's URL and headers.
- `routines/` - Everything that runs on a schedule: the routine definitions
  under `system/routines/`, the scheduler, the runner for command runs and
  skill runs (automations), the run ledger, the cron env wrapper, and the
  `routines` command (see the manage-routines skill and the Routines app).
- `bootstrap/` - First-boot setup; then launches supervisord, which supervises
  the apps and services.
- `chat_background_tasks/` - The marker files recording the background tasks
  each chat waits on, and their readers.
- `disk_headroom/` - How much the workspace can still write to its disk: the
  filesystem's free space, or the room left under a cloud machine's disk
  quota when that runs out first, and the agent-facing
  `uv run --no-sync disk-headroom` command that reports it.
- `github_sync/` - The opt-in GitHub auto-push wiring (a git hook, not a
  daemon; see the github-sync skill).
- `mngr_cli_contract/` - Shared validator that checks mngr CLI argvs against
  the live mngr command tree.
- `onboarding_activity/` - One-way marker files recording what the workspace's
  owner has already done (finished the tour, used the phone layout, started a
  second chat), and the four onboarding items the chat's new-chat suggestions
  and the onboarding checklist show from them.
- `pytest_executable_tmp/` - Test-only pytest plugin that keeps a session's
  temporary files where a stub executable written there can run, or stops
  the session before any test starts.
- `pytest_workspace_env/` - Test-only pytest plugin that clears the
  environment an mngr agent runs with before any test code runs, and, with
  `DWT_REQUIRE_WORKSPACE_ENV=1` set, fails rather than skips any test (the CI
  job that runs the suites inside the workspace image sets it).
- `tk_command_parsing/` - Parsing helpers for the vendored `tk` ticket
  tracker's command output.
- `workspace_backup_setup/` - A workspace's backup configuration as a client
  sets it up through the shell's backup routes: it initialises the restic
  repository the credentials name, writes `restic.env` once that is ready,
  and sets whether backups are verified.
- `workspace_initial_data/` - The document a workspace is handed when it is
  created (its profile snapshot, timezone, update settings and backup
  destination), the `apply-initial-data` step the boot sequence runs to apply
  it, and how that stands for the shell's status route.
- `workspace_update_control/` - A workspace's updates as a client asks for
  them through the shell's update routes (start one, schedule one, skip a
  version, write the update settings, read it all), and the dispatcher cron
  runs every minute through `system/scripts/update_dispatcher_tick.py` to arm
  an update to the version the workspace's release channel is on, start a
  scheduled update, and watch over a running one. Stdlib-only, so the tick
  runs with no venv.

Apps live in `system/apps/`; standalone background services live in
`system/services/`.
