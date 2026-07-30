Integration branch combining the workspace-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: backup settings move to `data/system/backup.toml`, the injected restic env to `data/.secrets/restic.env`, and the prune timestamp to `data/.state/last-restic-prune` (the package itself moves to `system/libs/host_backup`).
