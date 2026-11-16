The README runs the `workspace-themes` commands as `uv run --no-sync workspace-themes ...`, so a one-off never relocks or syncs the workspace.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.
