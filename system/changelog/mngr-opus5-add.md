Bump the pinned Claude Code version from 2.1.207 to 2.1.220 (Dockerfile, setup_system.sh, and the `[agent_types.claude].version` pin in .mngr/settings.toml), enabling the Claude Opus 5 model in workspaces. Lands together with the matching mngr PR that bumps the release Dockerfile pin and adds `claude-opus-5` to the LiteLLM proxy.

The composer's Opus entry is now labelled "Opus 5". It selects the `opus[1m]` alias, which Claude Code resolves to whichever Opus is current, so on the new pin that entry has been serving Opus 5 under the old "Opus 4.8" label.
