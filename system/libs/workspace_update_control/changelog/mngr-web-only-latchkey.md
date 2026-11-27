New library `workspace_update_control`: the Imbue Studio desktop app's in-workspace update program, moved into the template for the shell's update routes.

`control` has one function per operation: start an update now, arm the schedule, refresh or cancel it, skip a version, record a dismissal, read it all, and read whether a running update is alive. Starting answers `STARTING` at once, and `data/.state/update-apply/last-start.json` records how the create of the update's chat went. Reading changes nothing.

`dispatcher` is a port of the app's dispatcher and acts the same on the same state files. `agents` reads the workspace's agents from the events file `mngr observe` writes. The library is standard library only, so cron runs its tick with no venv.
