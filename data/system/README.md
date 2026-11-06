# data/system/

Workspace configuration written at runtime:

- `backup.toml` - Optional user settings for the continuous backup (interval,
  retention, excludes). Absent means built-in defaults. `extra_excludes` adds
  patterns to the defaults; `excludes` replaces them. Also carries
  `verification_disabled`, the Imbue Studio app's per-workspace switch for its
  own backup verification (the service ignores it).
- `profile/` - The workspace's own settings layer: `overrides/users/<user_id>.json`
  (one flat map of setting key to value per user, written by the workspace only;
  wins over the account's value for that user here) and `unsent.json` (account
  changes the shell accepted but has not yet delivered to the account). Read by
  `system/libs/workspace_profile/`.
- `github_sync.toml` - Present only when the opt-in GitHub sync is enabled;
  holds the private sync repo's URL.
