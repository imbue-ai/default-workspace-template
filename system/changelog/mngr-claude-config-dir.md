Breaking cutover: every claude in a workspace now uses claude's own default config dir, the shared `~/.claude`, instead of the services agent's per-agent `~/.mngr/agents/<id>/plugin/claude/anthropic/` dir. `CLAUDE_CONFIG_DIR` is deliberately unset workspace-wide, so mngr-launched agents and a bare `claude` in a workspace terminal share the same auth, plugins, marketplaces, and sessions. Existing workspaces are not migrated -- re-create them.

The `main` (services) agent type is now a plain `command` agent (`parent_type = "command"`, `command = "sleep infinity"`), not a claude agent: no claude ever runs as it, so it no longer receives a per-agent `CLAUDE_CONFIG_DIR` that supervisord services and `mngr create` calls would inherit, and the unreachable `&& claude` window-0 hack is gone.

`skipDangerousModePermissionPrompt = true` moved into `agent_types.claude.settings_overrides` (the mngr-managed `--settings` overlay), since nothing provisions a shared settings.json anymore.

`setup_system.sh` now fails the image build / VM provision immediately when the installed `claude --version` does not match the pinned `CLAUDE_CODE_VERSION`, and a new `system/test_workspace_claude_config.py` pins the three version-pin locations to each other, asserts settings.toml never exports `CLAUDE_CONFIG_DIR`, and asserts the `main` type resolves to the plain command agent.
