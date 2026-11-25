The workspace routes gain what a client with no backend of its own needs, and a workspace arms its own updates.

- New route `PUT /api/workspace/update/settings` (operation `update.settings.write`) stores the update settings: the release channel the workspace follows, the feed its file is read from, whether the workspace arms its own updates (the account's answer, and this workspace's own, which wins), and the owner's local hours an update may start in.

- `ceiling`, the window and `is_backup_configured` are optional in `POST /api/workspace/update/start` and in `PUT` and `PATCH /api/workspace/update/schedule`. A value left out is the workspace's own: the version its release channel is on, the window of its update settings, and whether it holds backup credentials. With no version to be had the answer is the outcome `NO_CHANNEL_VERSION`, and with no settings to take a window from `NO_UPDATE_SETTINGS`. A request that names them is served as before.

- `GET /api/workspace/update` also answers `settings` and `channel_version`: the version the settings' channel is on as the workspace last read it, when, and why the last read failed if it did.

- `GET /api/workspace/status` answers `compatibility`, the template's compatibility number (1), which is raised only by a change an older client could not work with.

- `GET /api/workspace/sharing` lists `shareable_apps`: the registered apps that are not internal, each with its name, its origin label, its display name and whether it is a system app.

- `PUT /api/workspace/profile` leaves the stored snapshot alone when the one sent equals it apart from `written_at`, and answers `is_written: false`.

`workspace_routes_api.json` is regenerated, and no released copy is broken.
