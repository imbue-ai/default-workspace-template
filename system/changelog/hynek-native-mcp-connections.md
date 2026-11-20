Add the `share-grants` dependency.

`.mngr/settings.toml` keeps `ENABLE_CLAUDEAI_MCP_SERVERS=false` in the host's env file as the workspace's one switch for the provider accounts' own cloud connections, and starts each codex agent with `-c features.apps=${ENABLE_CLAUDEAI_MCP_SERVERS:-false}` (`cli_args` under `[agent_types.codex]`) so codex follows the same line.
