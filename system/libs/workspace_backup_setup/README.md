# workspace-backup-setup

A workspace's backup configuration as a client outside it sets it up. The shell's
workspace routes (`/api/workspace/backups/...`) are the callers; the `host-backup`
service is what then backs the workspace up.

## Modules

- `setup` -- takes the contents of `data/.secrets/restic.env` from a client,
  initialises the restic repository they name in the background, and writes the
  file only once the repository is ready, so the backup service never sees
  credentials for a repository that is not initialised. A file with the same
  contents is left alone, and one with other contents is moved to
  `restic.env.<timestamp>` first. A repository that already exists is success when
  the password opens it. The state of the initialisation (`NONE`, `INITIALIZING`,
  `READY`, `FAILED`) lives in `data/.state/backup_setup/state.json` and names the
  process that runs it, so a state left at `INITIALIZING` by a process that is gone
  reads as `FAILED`. While it runs, the credentials sit in
  `data/.secrets/restic.env.pending` (mode 0600), removed when it ends. No state and
  no failure detail ever holds a credential. Removing the credentials moves
  `restic.env` aside and starts the state over.
- `verification` -- reads and sets `verification_disabled` in
  `data/system/backup.toml`, editing that one line and keeping every other line of
  the user's file as it is.

The parser of `restic.env` and the restic wrapper are the backup service's own
(`host_backup.config`, `host_backup.restic`).
