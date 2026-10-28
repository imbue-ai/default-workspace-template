`update-self` carries the Style Guide frontend's bundle like the Routines app's: a change under `system/apps/style_guide/frontend/` rebuilds, snapshots and verifies it, and a worker hands it in with `--worker-bundle style_guide=<path>`.

`manage-desktop` says an app whose manifest declares `desktop_shortcut = false` gets no default shortcut on any desktop, and stays in the launcher.
