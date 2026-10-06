`claude_p.py` works from a supervisord service or a cron job again.

- Every call failed with "OAuth session expired and could not be refreshed". With `CLAUDE_CONFIG_DIR` unset, the helper read credentials from the workspace's default account, but the `claude -p` it launched still looked in `~/.claude`, which holds no sign-in. The child now runs on the same account the resolver reads: the agent's own account inside a chat, else the workspace default.

- Every call also waited 3 seconds. Under supervisord the child inherited a stdin that never closes, so `claude -p` waited for input before proceeding. The helper now gives it a closed stdin.

- The helper's docstring and `references/billing-and-credentialing.md` no longer say `claude -p` authenticates from the shared `~/.claude`, and the `migrate-workspace` layout reference no longer says the resolver reads the shared Claude settings.

- With `CLAUDE_CONFIG_DIR` unset, the helper now picks the workspace's default Claude account the way a new chat does: the pinned default, else the most recently used one. It used to take the most recently used one even when another was pinned as the default.
