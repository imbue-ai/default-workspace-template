# system/scripts/

Provisioning and utility scripts:

- Image build / provisioning: `setup_system.sh`, `install_dependencies.sh`,
  `build_workspace.sh`, `write_apt_sources.sh`, `seed_home_skeleton.sh`,
  `default_workspace_template_seed.sh`, `install_secret_scanners.sh`,
  `_provision_guard.sh`, `_tool_env.sh`, `install_mngr.py`, `tool_env.py`
  (vendored byte-identically into `.agents/skills/update-self/scripts/`, which
  the apply runs as a self-contained unit), and the boot-convergence units in
  `env.d/`.
- Cross-harness agent policy hooks (`agent_*.sh` / `agent_*.py`), wired in
  `.claude/settings.json` for claude and `.codex/hooks.json` for codex; pi's
  `.pi/extensions/policy_guards.ts` and agy's `agy_shim/bash` feed the same
  scripts the payload they read on stdin. See `tool-call-policies.md` for what
  each one enforces.
- Plugin provisioning: `install_agent_plugins.sh` (run by `setup_system.sh`)
  puts the agent plugins under `/opt/agent-plugins/` as local marketplaces, at
  commits it pins: code-guardian, and Anthropic's `frontend-design` under a
  marketplace of our own (`claude-code-plugins-pinned`, since Claude reserves
  the official name for GitHub sources). `.claude/settings.json` declares them,
  and Claude loads them in place, so no session or account installs anything
  and nothing is fetched at runtime; a pin bump reaches running workspaces when
  update-self re-runs `setup_system.sh`. `codex_update_plugin.sh` installs
  code-guardian into each mngr Codex agent's isolated home from the same local
  copy, after its config is written and before launch.
  Re-provisioning restores plugin enablement after mngr rewrites the config.
  Install failures warn without blocking launch; use `--strict` for verification.
  The marketplace source and plugin enablement are declared in the Codex agent
  type's `config_overrides`. Existing reviewer settings still control gates;
  the worker's disabled review/CI/fetch gates are unchanged. mngr's existing
  workspace-trust consent governs hook execution.
- Claude status line: `claude_status_line.sh`, wired in `.claude/settings.json`. It prints
  nothing; it records each agent's live model state for the chat model bar.
- Utility scripts: `forward_port.py` (port registry), `message_chat.py` (send a
  message to a chat by its id through the chat app, with `mngr message` as the backoff; the in-workspace
  replacement for `mngr message <agent>`; `--create` makes a new chat there
  the same way, with `mngr create` as the backoff), `run_in_background.py` (run a
  command detached and, when it exits, send its exit code and output to the
  caller's own chat as a message that starts the agent's next turn, on any
  harness; how a lead waits for a worker's report; vendored byte-identically into
  `.agents/skills/update-self/scripts/`, whose staged copy update-self waits
  through), `background_tasks.py` (the background tasks each chat is waiting
  on, one marker file per task under `data/.apps/chat/background_tasks/<chat-id>/`,
  written by `run_in_background.py` and by Claude's Stop hook; `list` and
  `is-busy` ask the chat app and fall back to the files; a thin entry point over
  the `system/libs/chat_background_tasks` library, which everything else imports),
  `claude_background_task_hooks.py` (Claude's Stop and SessionStart hooks,
  wired in `.claude/settings.json`, that record Claude's own background tasks as
  markers; the Claude-specific logic lives in this script, over the same
  library's markers),
  `with_secrets.py` (run a
  command with one `data/.secrets/<name>.env` file's variables in its
  environment; the one sanctioned reader of that directory, which
  `agent_secrets_guard.sh` enforces -- see the `connect-external-service`
  skill), `seed_welcome_chat.py` (open the workspace's first chat on the
  conversation the Imbue Studio app had before the workspace existed; run through
  `mngr exec` by the Imbue Studio app), `require_create_account.py` (the
  create gate), `refresh_workspace_view.py` (rebuild the user's view after the
  interface changes), `migrate_claude_auth.py` (one-time auth migration).
- Boot recovery: `minds_start_services_agent.sh`, `minds_lima_autostart.sh`.
- The changelog gate: `check_changelog_entries.py`.

Cohesive machinery lives in packages instead: the recurring-job/automation
scripts in `system/libs/automations/`, the Caretaker check in
`system/services/caretaker/`, the OOM entry points in
`system/services/oom_priority/bin/`, the terminal tmux helpers in
`system/apps/terminal/`, and the github-sync git hook in
`system/libs/github_sync/git_hooks/`.
