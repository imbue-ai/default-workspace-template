# system/scripts/

Provisioning and utility scripts:

- Image build / provisioning: `setup_system.sh`, `install_dependencies.sh`,
  `build_workspace.sh`, `write_apt_sources.sh`, `seed_home_skeleton.sh`,
  `default_workspace_template_seed.sh`, `install_secret_scanners.sh`,
  `_provision_guard.sh`, and the boot-convergence units in `env.d/`.
- Claude Code hooks (`claude_*.sh` / `claude_*.py`), wired in
  `.claude/settings.json`.
- Utility scripts: `forward_port.py` (port registry), `layout.py` (dockview
  layout ops), `migrate_claude_auth.py` (one-time auth migration).
- Boot recovery: `minds_start_services_agent.sh`, `minds_lima_autostart.sh`.
- The changelog gate: `check_changelog_entries.py`.

Cohesive machinery lives in packages instead: the recurring-job/automation
scripts in `system/libs/automations/`, the Caretaker check in
`system/services/caretaker/`, the OOM entry points in
`system/services/oom_priority/bin/`, the eval worker in
`system/services/eval_worker/`, the terminal tmux helpers in
`system/apps/terminal/`, and the github-sync git hook in
`system/libs/github_sync/git_hooks/`.
