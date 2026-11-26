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

## Modules

- `apply` -- reads the document and hands each part to its writer. When every part
  is written it records that and deletes the document, which holds storage
  credentials. A document that does not parse, whose `schema` is above the one this
  template knows, or a part of which its writer refuses, is left where it is with
  the reason recorded, and the next start applies it again whole: every writer is
  safe to run twice. The backup part is taken by the function the caller passes
  in.
- `status` -- how the initial data stands, kept in
  `data/.state/initial_data/state.json` and answered by the shell's
  `GET /api/workspace/status` as its `initial_data` member: `NONE` (no document was
  found and none has been applied), `APPLIED` with when, or `FAILED` with why. No
  detail quotes a credential of the document.
- `cli` -- the `apply-initial-data` entry point. It hands the backup part to the
  workspace's own backup setup (`request_backup_destination` of
  `workspace_backup_setup`), exactly as `PUT /api/workspace/backups/destination`
  does: the destination and the storage that came with it are recorded, and the
  `backup-setup` program sets them up once the services run. A destination that
  function refuses fails the part, and so does one named while a backup operation
  holds the backup lease, which the next start then records.
