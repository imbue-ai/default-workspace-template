`system/scripts/` is broken apart: cohesive script clusters move into proper packages, leaving scripts/ with provisioning, Claude Code hooks, boot recovery, a few utilities, and the changelog gate.

The moves (each detailed in its own project's changelog): the eval worker to `system/services/eval_worker/`, the recurring-job machinery to `system/libs/automations/` (with `run_schedule_agent.sh` renamed `run_automation.sh` and the `schedule_agent` label/template renamed `automation`), the Caretaker check to `system/services/caretaker/`, the OOM entry points to `system/services/oom_priority/bin/`, and the github-sync post-commit hook to `system/libs/github_sync/git_hooks/`.

The terminal tmux helpers (`notify_terminal_session.py`, `terminal_tmux.conf`) move to `system/apps/terminal/`; `system/supervisord.conf`, `.mngr/settings.toml`, and `.claude/settings.json` point at all the new paths.

Vocabulary docs (root README, CLAUDE.md, workspace-internals) now link "automation" to the `system/libs/automations/` machinery, with the Caretaker as the built-in example.

This is a non-backwards-compatible cutover: existing hosts are not migrated (all users get new hosts).
