Each backup tick refreshes the environment record with `uv run --no-sync env-converge capture`, so a tick can no longer relock or sync the workspace. The slow-backup hint points at `uv run --no-sync host-backup-heavy-dirs`.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

The comment on the backup's exclusion of the update apply's snapshot directory now lists what that directory holds (the mngr tool's environment, `node_modules` and the built bundles): apps no longer have uv tools of their own to copy aside, and the venv is swapped by link rather than copied. Backups are unchanged.
