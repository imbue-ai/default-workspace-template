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
  `.claude/settings.json` for claude and `.codex/hooks.json` for codex; pi
  spawns their `*_check.py` checkers from `.pi/extensions/`. See
  `tool-call-policies.md` for what each one enforces.
- Claude Code features with no counterpart on the other harnesses
  (`claude_status_line.sh`, `claude_update_plugin.sh`), wired in
  `.claude/settings.json`.
- Utility scripts: `forward_port.py` (port registry), `layout.py` (dockview
  layout ops), `message_chat.py` (send a message to a chat by its id through
  the chat app, with `mngr message` as the backoff; the in-workspace
  replacement for `mngr message <agent>`), `seed_welcome_chat.py` (open the
  workspace's first chat on the conversation the Mind app had before the
  workspace existed; run through `mngr exec` by the Mind app),
  `welcome_count.py` (the number of times the welcome skill has run, which
  varies its greeting), `require_create_account.py` (the
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
