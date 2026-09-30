Skills name `uv run workspace-layout` for the desktop's window ops instead of `python3 system/scripts/layout.py`, which is removed; `manage-desktop` also documents the new `show` subcommand.

- The update run's "surface my chat window" helper runs the new command, and falls back to the old script in a workspace that still has it (the first update onto this release starts from one).

- The isolated-instance preview's hint names the new command.
