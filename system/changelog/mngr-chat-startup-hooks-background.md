A new chat no longer waits for a plugin install/update or `uv sync --all-packages` before it can take its first message: neither runs at chat start any more.

- The agents' plugins are pinned, like the Claude and Codex CLIs. `system/scripts/install_agent_plugins.sh`, run by `setup_system.sh`, puts code-guardian and Anthropic's `frontend-design` under `/opt/agent-plugins/` at fixed commits, as local marketplaces. `frontend-design` sits in a marketplace of our own, `claude-code-plugins-pinned`, because Claude reserves the official marketplace name for GitHub sources.

- Claude loads both plugins in place from those folders, declared in `.claude/settings.json`, so no session, account or worker installs anything and nothing is fetched from GitHub at runtime. A brand-new Claude account's first chat has them. `claude_update_plugin.sh` and the two SessionStart hooks that ran it and `uv sync --all-packages` are gone, as is the worker's plugin-install provision step.

- New Codex agents install code-guardian from the local copy (about 0.16 s) instead of cloning it from GitHub during create (about 1.5 s, and once 93 s when the fetch hung).

- A plugin update now reaches workspaces through a pin bump in `install_agent_plugins.sh`, which update-self applies by re-running `setup_system.sh`.
