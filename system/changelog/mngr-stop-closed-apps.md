Apps stop when nothing shows them, and come back on the next request (`docs/system/specs/stop-when-no-windows.md`).

- The shell stops an app that declares `stop_when_no_windows = true` in its manifest once no window on any desktop has shown it for a minute (and only once someone has visited the workspace), and holds the app's port while it is stopped: the first request for the app starts it again and is answered with a loading page that reloads into the app. Neither `mngr forward` nor the share gateway learns anything about starting apps. The file viewer, the browser, and Getting Started declare the field; the chat, the terminal, and the workspace shell are critical and never stop.

- "Quit <app>" on a window's menu closes every window of the app and stops it at once; the Stop and Start pair is gone, and so is the Start button on a stopped app's window.

- The root-venv services (`host-backup`, `share-gateway`, `env-converge`) run as their own process instead of under a resident `uv run` parent, which held about 19 MB each for the life of the workspace; program lines that ended in `&& <entry>` now `exec` it.

- The `app-watcher` service is gone: the shell, which already watches the app registry, announces registrations to the minds desktop itself (the `service_registered` and `service_deregistered` events under `$MNGR_AGENT_STATE_DIR/events/services/`), and re-reads the registry on its sweep when the file's mtime moved, the backstop the watcher's polling provided.

- Existing workspaces: an app nobody has a window on stops a minute after the workspace is next visited, and starts again the moment it is opened; the `app-watcher` program is removed at the next update.
