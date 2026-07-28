# system/scripts/

Provisioning and utility scripts:

- Image build / provisioning: `setup_system.sh`, `install_dependencies.sh`,
  `build_workspace.sh`, `write_apt_sources.sh`, `seed_home_skeleton.sh`,
  `default_workspace_template_seed.sh`, `install_secret_scanners.sh`,
  `_provision_guard.sh`, and the boot-convergence units in `env.d/`.
- Claude Code hooks (`claude_*.sh` / `claude_*.py`), wired in
  `.claude/settings.json`.
- Service helpers: `forward_port.py` (port registry), `layout.py` (dockview
  layout ops), `oom_tag_service.py` and friends (memory-pressure bands),
  `minds_start_services_agent.sh`, `minds_lima_autostart.sh`.
- Git hooks in `git_hooks/` (activated by the github-sync skill).
- The changelog gate: `check_changelog_entries.py`.
