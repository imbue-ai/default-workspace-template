- The workspace routes (`/api/workspace/...`) can be called by the hosted web client's page: the shell names them to the request guard, which admits a request of another origin to them when the share gateway vouches that the web client's page sent it. Every other route of the shell refuses that page as it refuses any other origin's.

The workspace routes gain what a client with no backend of its own needs, and a workspace arms its own updates.

- New route `PUT /api/workspace/update/settings` (operation `update.settings.write`) stores the update settings: the release channel the workspace follows, the feed its file is read from, whether the workspace arms its own updates (the account's answer, and this workspace's own, which wins), and the owner's local hours an update may start in.

- `ceiling`, the window and `is_backup_configured` are optional in `POST /api/workspace/update/start` and in `PUT` and `PATCH /api/workspace/update/schedule`. A value left out is the workspace's own: the version its release channel is on, the window of its update settings, and whether it holds backup credentials. With no version to be had the answer is the outcome `NO_CHANNEL_VERSION`, and with no settings to take a window from `NO_UPDATE_SETTINGS`. A request that names them is served as before.

- `GET /api/workspace/update` also answers `settings` and `channel_version`: the version the settings' channel is on as the workspace last read it, when, and why the last read failed if it did.

- `GET /api/workspace/status` answers `compatibility`, the template's compatibility number (1), which is raised only by a change an older client could not work with.

- `GET /api/workspace/sharing` lists `shareable_apps`: the registered apps that are not internal, each with its name, its origin label, its display name and whether it is a system app.

- `PUT /api/workspace/profile` leaves the stored snapshot alone when the one sent equals it apart from `written_at`, and answers `is_written: false`.

The backup routes are reshaped for a workspace that sets up its own backups. `PUT` and `DELETE /api/workspace/backups/credentials` and `GET /api/workspace/backups/setup`, which no release froze, are removed with their operations.

- `GET /api/workspace/backups` (`backups.read`) answers the destination, how the setup stands (with `AWAITING_STORAGE`, and `retry_at` after a failure), whether `restic.env` is in place, the verification flag, the repository, the digest of the credentials in use, and who holds the backup lease.

- `PUT /api/workspace/backups/destination` (`backups.destination.write`) names `IMBUE_CLOUD`, `OWN_STORAGE` or `NONE`, with the storage where the client has some, and answers the state under 202 or 200. It is refused with `INVALID_DESTINATION` for a typed password or an unknown destination, and with 409 `BACKUP_OPERATION_RUNNING`, naming the holder, while the backup lease is held.

- `GET /api/workspace/backups/credentials` (`backups.credentials.read`) answers the text of the `restic.env` in use and its digest to the owner alone, and 403 `OWNER_ONLY` to anyone else.

`workspace_routes_api.json` is regenerated, and no released copy is broken.
