New library `workspace_backup_setup`, for the shell's backup routes.

`setup` takes the contents of `restic.env` from a client, initialises the restic repository they name in the background, and writes the file only once the repository is ready. Its state (`NONE`, `INITIALIZING`, `READY`, `FAILED`) lives in `data/.state/backup_setup/state.json`, a setup whose process is gone reads as `FAILED`, and removing the credentials moves `restic.env` aside. No state or failure detail holds a credential.

`verification` reads and sets `verification_disabled` in `data/system/backup.toml`, keeping every other line of the file.
