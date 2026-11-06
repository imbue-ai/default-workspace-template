The README and the CLI's module help document the subcommands as `uv run --no-sync env-converge ...`.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

env-converge no longer reinstalls a recorded uv tool whose name is one of this workspace's apps (the `[project]` name of a package under `system/apps/`). Apps now run only from the workspace venv, so such an entry is an old per-app tool, and the package index's project of that name belongs to someone else (`system-monitor` 0.1.0 is one). A record captured before an update removed those tools would otherwise have installed it as root on a rebuilt rootfs.
