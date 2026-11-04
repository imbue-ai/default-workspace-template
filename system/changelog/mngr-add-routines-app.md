Routines: everything that runs on a schedule is now a routine, one tracked TOML file under `system/routines/<id>.toml` (the root `routines/` symlink), run by the new routines library and shown by the new Routines app. The design is `docs/system/specs/routines/spec.md`.

- The hand-written cron entries, the `run_job.sh` runner and the `run_automation.sh` wake script (`system/libs/automations`) are gone; the bootstrap writes one `routines` cron entry (the once-a-minute tick) at every boot and converts a workspace's old `data/.state/cron.d` entries into definitions, once.

- The Caretaker is the routine `caretaker` (`system/routines/caretaker.toml`), off by default and turned on from the Routines app; its own create template is gone (the `automation` template serves every skill routine), and its check now also reports routines that are failing, overdue, broken or waiting on the user.

- The Routines app (`system/apps/routines`, port 8050, launcher rank 45, its own memory band) joins the built-ins and the npm workspace; the root `uv.lock` and `system/package-lock.json` follow.

The changelog gate keys a project by its parent and name (`libs/routines`, `apps/routines`), so a library and an app of the same name each owe their own entry; before, both collapsed into one project whose entry was always the library's.

The image's dependency layer copies the two new workspace members' `pyproject.toml`s and the Routines frontend's `package.json` with the other manifests, so the cached install sees them.
