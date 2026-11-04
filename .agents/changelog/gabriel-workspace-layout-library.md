Skills name `uv run --no-sync workspace-layout` for the desktop's window ops instead of `python3 system/scripts/layout.py`, which is removed; `manage-desktop` also documents the new `show` subcommand.

- The isolated-instance preview's hint names the new command.

- `manage-desktop` places windows with `workspace-layout place --state snapped-left|snapped-right|maximized`.

- `update-app` and `update-self` treat a change under `system/libs/workspace_layout/` as a change to a critical app: it is made in an isolated worktree, never the served tree, takes the `system_interface`, `chat`, and `terminal` editing leases, counts in the freshness check, and previews as the shell with the chat.

- `migrate-workspace` lists every `layout.py` call it carries over among the legacy paths to fix, since the script it names is gone and its flags changed.
