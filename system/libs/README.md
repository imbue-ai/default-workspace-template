# system/libs/

Support libraries: workspace packages that are neither apps (no tab) nor
background services (no supervised program of their own). Each is a uv
workspace member with its own `pyproject.toml`; see each package's README for
details.

- `automations/` - The machinery that runs automations (skills on a
  schedule): the durable recurring-job runner, the cron env wrapper, and the
  automation-agent waker (see the manage-scheduled-tasks skill).
- `bootstrap/` - First-boot setup; then launches supervisord, which supervises
  the apps and services.
- `github_sync/` - The opt-in GitHub auto-push wiring (a git hook, not a
  daemon; see the github-sync skill).
- `mngr_cli_contract/` - Shared validator that checks mngr CLI argvs against
  the live mngr command tree.
- `tk_command_parsing/` - Parsing helpers for the vendored `tk` ticket
  tracker's command output.

Apps live in `system/apps/`; standalone background services live in
`system/services/`.
