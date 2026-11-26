- `system/test_web_client_routes.py` holds the share gateway and the shell to one another on what the hosted web client's page may call: the path prefix, the methods the browser's permission check allows, and the name of the mark on the request identity.

New entry file `system/scripts/install_update_dispatcher_cron.py`: it writes the `update-self-dispatch` cron line that runs the update dispatcher, for the bootstrap, which runs it at every boot with the system `python3` and no venv. It joins the bare entry lists.

The desktop interface's contracts, section 5.7, describe the update settings route (with the workspace's own hours, and the rule that keeps a setting of the workspace's own that a request leaves out), the workspace arming its own updates from its release channel's version, the request values a client may now leave to the workspace, how a desktop that still names its newest version holds the workspace to it, the compatibility number, the shareable apps, the profile route leaving an unchanged snapshot alone, the initial data a workspace is handed at create and how the status answers it, and the routes for the suggestion switches, the apps and the wallpaper. Section 2 gains the manifest's `suggestion_settings_path`.

New workspace member `system/libs/workspace_initial_data`, with its entry in the lockfile.

The state docs list the files a workspace that arms its own updates keeps under `data/.state/update-apply/`: `settings.json`, `channel-version.json`, `client-ceiling.json`, `schedule-origin.json` and `installed-version.json`, and where the initial data's state is kept, `data/.state/initial_data/state.json`.

New program `system/scripts/backup_lease.py` (`backup-lease`): it takes, renews, gives back and reads the workspace's backup lease, a file at `/run/backup-operation.lease` that keeps two backup operations apart, and `hold` runs a command under it, renewed every minute. The workspace's own backup setup takes it, and the Imbue Studio desktop's backup programs run it by its path with no venv. It joins the bare entry lists.

New supervised program `backup-setup` (`system/supervisord.conf.d/backup_setup.conf`, which supervisord knows as `backup_setup`), which sets up the workspace's backups.

The desktop interface's contracts, section 5.7, also describe the workspace setting up its own backups: the destination a client names, the backup state, the read of the credentials by the owner, a `restic.env` another writer put in place, and the backup lease. The state and secrets docs list what the backup setup keeps.
