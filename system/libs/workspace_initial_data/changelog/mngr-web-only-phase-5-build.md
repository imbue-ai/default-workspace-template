New library: the document a workspace is handed when it is created, and the step that applies it.

- Whoever creates a workspace leaves `$MNGR_HOST_DIR/initial_data.json` (schema 1): the profile snapshot, the timezone, the update settings and the backup destination, each in the shape of the request of the workspace route that writes the same thing. A part left out, or null, writes nothing.

- `apply-initial-data` hands each part to the writer its route calls. When every part is written it records that and deletes the document, which holds storage credentials. A document that does not parse, whose schema is above 1, or a part of which its writer refuses, is left where it is with the reason recorded, and the next start of the workspace's services applies it again whole.

- `status` keeps how it stands in `data/.state/initial_data/state.json` (`NONE`, `APPLIED` with when, `FAILED` with why), which the shell's status route answers. No reason quotes a value of the document.

- The backup destination is handed to a function of `workspace_backup_setup` that the caller passes in. With none passed, a document that names a destination fails rather than being skipped.
