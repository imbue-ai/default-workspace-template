New library: the document a workspace is handed when it is created, and the step that applies it.

- Whoever creates a workspace leaves `$MNGR_HOST_DIR/initial_data.json` (schema 1): the profile snapshot, the timezone, the update settings and the backup destination, each in the shape of the request of the workspace route that writes the same thing. A part left out, or null, writes nothing.

- `apply-initial-data` hands each part to the writer its route calls. When every part is written it records that and deletes the document, which holds storage credentials. A document that does not parse, whose schema is above 1, or a part of which its writer refuses, is left where it is with the reason recorded, and the next start of the workspace's services tries again.

- A timezone the workspace's tz database does not know (`Etc/Unknown`, or a zone newer than the image's tzdata) is no refusal: the part is recorded as applied and no zone is written, so the workspace keeps UTC until a client writes a zone through `PUT /api/workspace/timezone`. A `timezone` part of the wrong shape is still refused.

- Each part is applied at most once, so a later start never puts the create's value back over what the owner chose since, and never names the create's backup destination again after the owner turned backups off. The step records, under the digest of the document, that a part is about to be written before it writes it and that it was written afterwards. A later start tries only the parts that were refused. A part the step was interrupted in the middle of is not written again, and reads `FAILED` from then on.

- `status` keeps how it stands in `data/.state/initial_data/state.json` (`NONE`, `APPLIED` with when, `FAILED` with why, naming each part that is not written), which the shell's status route answers. No reason quotes a credential of the document.

- The backup destination is handed to the workspace's own backup setup (`request_backup_destination` of `workspace_backup_setup`), as the destination's route hands it one: it is recorded with the storage that came with it, and `backup-setup` sets it up once the services run. A destination the setup refuses fails that part, and so does one named while a backup operation holds the backup lease.
