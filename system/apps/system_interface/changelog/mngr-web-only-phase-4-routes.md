The shell serves the workspace routes (`/api/workspace/...`): the state a workspace owns, read and changed over HTTP by a client outside it, where the Imbue Studio desktop app used to run a command in the workspace.

- `GET /api/workspace/status` names the version of the checkout and every operation the shell has a route for; a client calls a route only for an operation named there, and a preview shell names none.

- Sharing: `GET /api/workspace/sharing` reads whether the workspace holds share materials and its grants, and `PUT /api/workspace/sharing/grants` replaces the grants and the identities beside them, checking the document's shape.

- Profile: `PUT /api/workspace/profile` writes the profile snapshot, and `PUT /api/workspace/timezone` records the user's timezone.

- Backups: `PUT` and `DELETE /api/workspace/backups/credentials` take or move aside `restic.env`, `GET /api/workspace/backups/setup` says how the initialisation of the repository stands, and `PUT /api/workspace/backups/verification` turns verification on or off. The repository is initialised in the workspace, and `restic.env` is written only once it is ready.

- Updates: `GET /api/workspace/update`, `POST .../update/start`, `PUT`, `PATCH` and `DELETE .../update/schedule`, `POST .../update/skip`, `POST .../update/dismissals` and `GET .../update/liveness`. A start answers at once, and the read then says how creating the update's chat went.

`workspace_routes_api.json` describes these routes for the clients that call them, each with the name of the operation it serves. A test keeps it current and holds it to every released copy under `workspace_routes_api_frozen/`. A release writes its copy there in the commit it tags, and the tag gate fails a tag without one.
