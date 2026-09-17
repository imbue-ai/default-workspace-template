`minds-admin cutover` is reworked from the flag-day drain/repave/restore stages into the incremental gen-1 -> gen-2 migration: `cutover migrate` moves selected workspaces (per `--workspace`, `--user`, or `--source-server-id`) onto one named gen-2 target box, sequentially -- live-harvesting keys/inspect/version (admin-starting stopped rows first), running the product's own stop for a verified artifact, copying it to a rollback prefix, parking the row, transplanting the data disk at freshly picked ports, replaying the container, and re-leasing at the new coordinates.

New `cutover rollback` returns one migrated workspace to gen-1 through the product's own restore, from the saved artifact copy; work done on gen-2 after the migration is lost by policy.

The migrate's final re-lease also stamps the harvested VM and container host keys onto the pool row: the replay puts exactly those keys on the new endpoints, and the row's recorded (bake-time) values can predate a client-side adopt rotation -- clients that pin host keys from the synced workspace record would otherwise hard-reject SSH to the migrated workspace.

`cutover repave` now requires explicit `--server-id`s and refuses a box that still holds pool rows or is referenced by an in-flight migration; `cutover preflight` becomes a per-row migrate-eligibility inventory (the tier-refusal fit logic is gone, and stopped rows are candidates).

Bakes are guarded by the release/generation pairing: a gen-2 box refuses `minds-v*` tags below 0.6.0 and a gen-1 box refuses newer ones (dev branches and the image-seed bake are exempt), so the release channels cohort each client version onto its generation.

Parallel migrations run as separate invocations with disjoint target boxes, enforced by per-target-box and per-workspace locks in the state dir.
