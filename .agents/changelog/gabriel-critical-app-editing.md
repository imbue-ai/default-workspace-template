`service-processes.md` describes the `agent-observer` program: the workspace's one `mngr observe`, a manifest-less supervised service with no app directory, which every chat instance follows and which a chat never starts on its own.

The build-app scaffold writes the `[preview]` table into every new app's `app.toml`: the library's default for the name spelled out (`<PACKAGE_UPPER>_PORT`, `_HOST`, `_DATA_DIR` over a copy of `data/.apps/<name>`), so an edit to the runner's env names has the table to keep in step beside it.
