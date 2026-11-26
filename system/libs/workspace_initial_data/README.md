# workspace-initial-data

The document a workspace is handed when it is created, and the step of the boot
sequence that applies it.

Whoever creates a workspace (the Imbue Studio desktop through `mngr`, or the
connector for a create from the web) leaves one JSON file of mode 0600 in the
machine's host directory, `$MNGR_HOST_DIR/initial_data.json`, as a cloud machine is
left its user data. The workspace applies it by itself, so no client has anything
to finish once the create has ended.

```json
{
  "schema": 1,
  "profile": { "snapshot_format": 1 },
  "timezone": { "zone": "Europe/Berlin" },
  "update_settings": {
    "channel": "stable",
    "feed_base_url": "https://updates.example",
    "is_auto_update_enabled": true,
    "window_start_hour": 2,
    "window_end_hour": 5
  },
  "backups": { "destination": "IMBUE_CLOUD" }
}
```

Each part has the shape of the request of the workspace route that writes the same
thing, and is handed to the writer that route calls: `profile` of
`PUT /api/workspace/profile`, `timezone` of `PUT /api/workspace/timezone`,
`update_settings` of `PUT /api/workspace/update/settings`, and `backups` of
`PUT /api/workspace/backups/destination`. A part that is left out, or null, writes
nothing.

## CLI

- `apply-initial-data` - Apply the document in `$MNGR_HOST_DIR`, when there is one.
  The boot sequence (`system/libs/bootstrap`) runs it from the workspace's root each
  time the workspace's services are started, after the workspace's Python packages
  are in place and before the clock is set, so a timezone the document carries is in
  force from that very start. It exits 0 whatever it found.

The trigger is the file being there, not a first boot: a pool machine boots when it
is baked, before it has an owner, and the document is written while the machine is
adopted, before its services are started again.

## Each part is applied at most once

Between two starts the workspace's services run, and the owner changes what the
document set: the update settings, the profile, where the backups go. A part
written a second time would put the create's value back over that choice, and a
second write of the backup destination would turn backups on again after the
owner turned them off. So a document one of whose parts was refused is left where
it is, and the next start tries the refused parts and no other.

The step keeps, in its record, the SHA-256 of the document and how far each part
has come. It records that a part is about to be written (`WRITING`) before it
calls the part's writer, and that it was written (`WRITTEN`) afterwards; each
record replaces the file in one step and is on disk before the step goes on. A
later start skips every part the record names. A part a writer refused is taken
out of the record again, which is what leaves it to be tried.

A part found as `WRITING` at a later start was being written when the step ended
(it was killed, or ran out of the time the boot gives it), and nobody knows
whether the write happened. It is not written again: the boot goes on when the
step dies, so the services may have run since. The status reads `FAILED` for that
part from then on, and the record says so from the moment the write begins, so it
is true even if the step never runs again. A client that opens the workspace
writes the settings it holds, as it does for any workspace, and the Backup page
offers the backups.

The order is the reason for the record: the mark comes before the write, since a
mark made after it could be lost with the step, and the part would then be
written twice. Rewriting the kept document without the parts already written
would need the same mark first, to survive the same moment, and would then leave
two files to keep in step.

Once no part is left to try, the document is deleted, also when a part reads as
interrupted, since it holds storage credentials and nothing more will be read
from it. A document left again with the bytes of one already applied is deleted
and writes nothing; a document with other bytes is a new one, and is applied.

## Modules

- `apply` -- reads the document and hands each part to its writer. When every part
  is written it records that and deletes the document, which holds storage
  credentials. A document that does not parse, whose `schema` is above the one this
  template knows, or a part of which its writer refuses, is left where it is with
  the reason recorded, and the next start tries again. The backup part is taken by
  the function the caller passes in.
- `status` -- how the initial data stands, kept in
  `data/.state/initial_data/state.json` and answered by the shell's
  `GET /api/workspace/status` as its `initial_data` member: `NONE` (no document was
  found and none has been applied), `APPLIED` with when, or `FAILED` with why,
  naming each part that is not written. No detail quotes a credential of the
  document. The same file holds what the step keeps for itself: the digest of the
  document it is applying, and how far each part of it has come.
- `cli` -- the `apply-initial-data` entry point. It hands the backup part to the
  workspace's own backup setup (`request_backup_destination` of
  `workspace_backup_setup`), exactly as `PUT /api/workspace/backups/destination`
  does: the destination and the storage that came with it are recorded, and the
  `backup-setup` program sets them up once the services run. A destination that
  function refuses fails the part, and so does one named while a backup operation
  holds the backup lease, which the next start then records.
