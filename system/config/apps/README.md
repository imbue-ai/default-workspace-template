# system/config/apps/

One record per app the workspace's agent built or installed: `<name>.toml`, written by the
`app-install` command (`system/libs/app_install/`). A record says which directory under
`system/apps/` is the app, the port its main program listens on, where the app came from, and
the repositories it is kept in step with.

The template ships none: every app here without a record is built in. Change a record with
`uv run --no-sync app-install`, not by hand.
