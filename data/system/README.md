# data/system/

Workspace configuration written at runtime:

- `backup.toml` - Optional user settings for the continuous backup (interval,
  retention, extra excludes). Absent means built-in defaults.
- `github_sync.toml` - Present only when the opt-in GitHub sync is enabled;
  holds the private sync repo's URL.
