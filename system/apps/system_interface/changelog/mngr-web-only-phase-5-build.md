- The workspace routes (`/api/workspace/...`) can be called by the hosted web client's page: the shell names them to the request guard, which admits a request of another origin to them when the share gateway vouches that the web client's page sent it. Every other route of the shell refuses that page as it refuses any other origin's.

The workspace routes gain what a client with no backend of its own needs, and a workspace arms its own updates.

- New route `PUT /api/workspace/update/settings` (operation `update.settings.write`) stores the update settings: the release channel the workspace follows, the feed its file is read from, whether the workspace arms its own updates (the account's answer, and this workspace's own, which wins), and the owner's local hours an update may start in (the account's, and this workspace's own, which win). A request that leaves one of the workspace's own three settings out keeps the stored value, and one that sends null clears it.

- `ceiling`, the window and `is_backup_configured` are optional in `POST /api/workspace/update/start` and in `PUT` and `PATCH /api/workspace/update/schedule`. A value left out is the workspace's own: the version its release channel is on, the window of its update settings, and whether it holds backup credentials. With no version to be had the answer is the outcome `NO_CHANNEL_VERSION`, and with no settings to take a window from `NO_UPDATE_SETTINGS`. A request that names them is served as before.

- `GET /api/workspace/update` also answers `settings` and `channel_version`: the version the settings' channel is on as the workspace last read it, when, and why the last read failed if it did.

- `GET /api/workspace/status` answers `compatibility`, the template's compatibility number (1), which is raised only by a change an older client could not work with, and `initial_data`: whether the workspace's boot applied the document it was handed when it was created (`NONE`, `APPLIED` with when, or `FAILED` with why).

- New routes `GET` and `PUT /api/workspace/suggestions` (`suggestions.read`, `suggestions.write`) read and change the workspace's two suggestion switches. The shell passes each request over loopback to the registered app that declares a `suggestion_settings_path`, sending a write exactly the switches the client named. An app that is not running or does not answer is `503 SUGGESTIONS_UNAVAILABLE`, and a workspace whose app is from before the switches could be reached this way is `501 SUGGESTIONS_UNSUPPORTED`.

- New route `GET /api/workspace/apps` (`apps.read`) lists the registered apps that are not internal, in the registry's order: what `shareable_apps` lists, each with the SVG markup of its icon.

- New route `GET /api/workspace/wallpaper` (`wallpaper.read`) answers the image the workspace's first desktop wears, with an `ETag` and `Cache-Control: private, no-cache`, and 204 when that desktop wears its theme's default. It is a workspace route like the others, so the request guard is no looser: an image element of another site is refused it.

- `GET /api/workspace/sharing` lists `shareable_apps`: the registered apps that are not internal, each with its name, its origin label, its display name and whether it is a system app.

- `PUT /api/workspace/profile` leaves the stored snapshot alone when the one sent equals it apart from `written_at`, and answers `is_written: false`.

`workspace_routes_api.json` is regenerated, and no released copy is broken.
