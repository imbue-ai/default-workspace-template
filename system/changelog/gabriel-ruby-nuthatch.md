- Fast mode is no longer on for every agent. `.mngr/settings.toml` now sets
  `fastMode = false` in the `settings_overrides__extend` for `[agent_types.claude]`,
  so every non-interactive agent type that inherits it (worker, worktree,
  subskill-worker) runs at standard speed. Fast mode buys latency at a higher
  per-token price, which only pays off when a human is waiting on the reply.

- User-facing chat agents instead get their fast-mode setting per create: both
  chat-create paths pass `-S agent_types.claude.settings_overrides.fastMode=<bool>`,
  resolved from the workspace's recorded decision. New chats start fast so the
  opening conversation feels responsive, and switch to whatever the user chose
  once they have answered the prompt.

- The override targets `claude` rather than `chat` because only the agent type
  that declares `settings_overrides` accepts a `-S` on it; a chat create resolves
  its own config, so overriding the base type there never reaches a worker create.

- The `CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE=1` host env var is now documented as
  vestigial: fast mode for Opus 4.7 was deprecated on 2026-06-25 and removed on
  2026-07-24, and this workspace runs Opus 4.8. It is left in place for pinned
  Claude Code versions older than the removal, and should be dropped on the next
  version bump.
