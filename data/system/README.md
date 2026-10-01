# data/system/

Workspace configuration written at runtime:

- `backup.toml` - Optional user settings for the continuous backup (interval,
  retention, excludes). Absent means built-in defaults. `extra_excludes` adds
  patterns to the defaults; `excludes` replaces them.
- `github_sync.toml` - Present only when the opt-in GitHub sync is enabled;
  holds the private sync repo's URL.
