# workspace-backup-setup

A workspace's own setup of its backups. A client outside the workspace (the Imbue
Studio desktop, or the hosted web client), or the workspace's initial data, names
where the backups go and supplies the storage credentials that destination needs.
The workspace does the rest: it makes the repository's password, initialises the
repository, installs `data/.secrets/restic.env`, and tries again by itself when any
of that fails. The `host-backup` service is what then backs the workspace up.

## The destination

A request is the body of `PUT /api/workspace/backups/destination`:

| `destination` | What comes with it |
|---|---|
| `IMBUE_CLOUD` | `imbue_cloud_storage`: `repository_url`, `rest_username` and `rest_password`, as the connector's `POST /backups/repositories` answers them |
| `OWN_STORAGE` | `own_storage`: `repository`, and `backend_env`, the variables of the storage as the user typed them |
| `NONE` | Nothing: backups are off |

The storage may be left out. The workspace then uses what it kept for that
destination, and awaits storage when it kept none. No request carries the
repository's password, and one that names `RESTIC_PASSWORD` among the variables of
the user's own storage is refused.

## Modules

- `destination` -- `request_backup_destination(workspace_root, body, *, lease_path,
  now, workspace_id)` records a request for `backup-setup` and answers the backup
  state. It is what the shell's route calls and what the workspace's initial data
  calls. It raises `InvalidBackupDestinationError` for a destination it cannot
  take, and `BackupOperationRunningError` for a request that would change
  something while another backup operation holds the backup lease. The
  workspace's own setup holding it refuses nothing: the request replaces what
  the setup was asked, and an attempt under way ends without installing
  anything. A request for what the workspace already holds changes nothing,
  except that a setup that had failed is tried again at once. Recording a
  request never touches the `restic.env` in use.
- `program` -- `backup-setup`, a supervised program
  (`system/supervisord.conf.d/backup_setup.conf`, where supervisord knows it as
  `backup_setup`). For a destination with storage it
  takes the backup lease, uses the password the workspace kept for that repository
  or makes one (kept on disk before it is used), initialises the repository or
  checks that the password opens the one already there, writes `restic.env`, and
  gives the lease back. A step that fails leaves the setup `FAILED`; it is tried
  again after a minute, then after twice as long each time, up to an hour. The
  exception is a storage that refuses the credentials it is shown: the setup
  then reads `AWAITING_STORAGE`, the state a client supplies storage for, and
  the storage that was offered is kept and tried again at the hourly check. For
  `NONE` it moves `restic.env` aside. Once an hour, and when it starts, it checks
  that the credentials in use still open the repository: a storage that refuses
  them leaves the setup `AWAITING_STORAGE` with `restic.env` where it is, and a
  check that fails for any other reason changes nothing. Storage offered for the
  repository already in use is taken only when the storage refuses what is held,
  since a repository at the backup store has one token.
- `state` -- the record of the setup, and `read_backup_state`, everything a client
  reads: the destination, how the setup stands (`NONE`, `AWAITING_STORAGE`,
  `INITIALIZING`, `READY`, `FAILED`, with what failed and when the next attempt
  starts), whether `restic.env` is in place, the verification flag, the
  repository, the digest of the credentials in use, and who holds the backup
  lease. `read_backup_credentials` answers the text of `restic.env` and its digest,
  for the route that answers them to the owner.
- `env_files` -- `restic.env`, the copies of it kept aside, and the credentials of a
  setup that has not finished.
- `verification` -- reads and sets `verification_disabled` in
  `data/system/backup.toml`, editing that one line and keeping every other line of
  the user's file as it is.

## What is on disk

- `data/.state/backup_setup/state.json` -- what the workspace was asked and how the
  setup stands. It holds no credential. A read of it that a write follows is
  serialized with that write by `state.lock` beside it (the destination request and
  the program); a client's read of the backup state takes no lock, so one made
  while the program installs the credentials can describe the moment between two
  of its files, and the next read is right.
- `data/.secrets/restic.env.pending` (mode 0600) -- the credentials of a setup that
  has not finished: the storage that was offered, and the repository's password
  once it is made.
- `data/.secrets/restic.env.<timestamp>` (mode 0600) -- every `restic.env` that was
  replaced or turned off, kept. A destination named again is installed from the
  newest one kept for it, and a repository named again is opened with the password
  kept for it.

Because the request is on disk, `backup-setup` takes its work up again whenever it
starts. To make it forget a setup it has not finished, remove `state.json` and
`restic.env.pending` while the program is stopped; the `restic.env` then in place
is taken as the destination. A pending file that names a repository and a
password is not deleted for that: it is kept aside as `restic.env.<timestamp>`,
since the repository may already be initialised with that password, and the
workspace finds it there when the repository is named again. The Imbue Studio
desktop's restore does all of this, so a workspace restored from a snapshot taken
during a change of destination does not take that change up again.

## A `restic.env` the program did not write

A released Imbue Studio desktop writes `restic.env` by its own command, and again
from its own copy when it updates the backup service. A current desktop's restore
and its migration of the backup store write it too; a current desktop's update of
the backup service writes none into a workspace that has the backup lease, which
every workspace with this program has. The record names the digest of the file as
the program last left it, so a file with another digest is taken as the
destination: the setup reads `READY` for the repository it names, and a setup the
program had not finished is dropped. The destination's kind is read off the file:
Imbue Cloud where the repository is a `rest:` address whose user name is the
workspace's own id (`MNGR_AGENT_ID`), and storage of the user's own otherwise. A
file the record expects that is gone is backups turned off.

## The backup lease

One backup operation runs in a workspace at a time. The lease that keeps them apart
is `system/scripts/workspace_bare_scripts/backup_lease.py` (a file at
`/run/backup-operation.lease`, ten minutes from when it was taken or last renewed).
`backup-setup` takes it as `setup`; the Imbue Studio desktop's restore, its update
of the backup service and its migration of the backup store take it through
`python3 system/scripts/backup_lease.py`.

## The backup service

The parser of `restic.env` and the restic wrapper are the backup service's own
(`host_backup.config`, `host_backup.restic`). The Imbue Studio desktop replaces
`system/services/host_backup` with the version it pins, so this library calls only
what every version of the service has: `RESTIC_ENV_PATH` and
`parse_restic_env_file`, `init_repo`, and `run_restic` with its `env_overrides` and
`timeout_seconds`.
