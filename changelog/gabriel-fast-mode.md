- Enabled Claude Code fast mode for all agents created from this repo by
  setting `fastMode = true` in the `settings_overrides__extend` for
  `[agent_types.claude]` in `.mngr/settings.toml` (was `false`). Because
  `settings_overrides` is applied last during mngr's Claude provisioning, this
  forces fast mode on for every agent type (claude/main/worker/chat/worktree),
  not just attended local ones. The `CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE=1`
  host env var that gates the capability was already present.
