- Added `minds-admin archives {candidates, create, list, links, download}`: archives the gen-1 workspaces the gen-2 migration cannot take (below the `minds-v0.3.10` floor, or without a `minds-v*` release tag) into the tier's workspace-storage bucket as one zip each, under `<prefix>archives/<host_id>/<stamp>/`, with a manifest beside it. `create` reads a running workspace without changing it (its volume from a read-only btrfs snapshot, plus the container's writable layer, streamed from the VM through the box's loopback SSH via `s5cmd`); `--start-stopped` admin-starts a stopped one first. `candidates` lists every gen-1 row with a retire/migrate verdict (live-probing running containers), `list` and `download` inspect the archives, and `links` mints 7-day download links into a support document (`~/.minds-<env>/archives/reports/archive-links-<stamp>.{md,csv}`), owner emails resolved from the SuperTokens core.

- Added `minds-admin workspaces retire <host_db_id>`: the product's admin stop stamped with the new `retired` kind (also accepted by `workspaces stop --kind`), for a workspace archived this way; nobody starts it again.

- The cutover drivers' shared box / VM / row helpers are now public (`OperatorBoxContext`, `vm_root_outer`, `vm_transfer_key`, ...) so the archive reuses them.

- The SuperTokens user listing behind `envs r2-cleanup` (and the archive's owner emails) now follows the core's pagination token instead of reading a single 500-user page, so a core with more accounts than that no longer under-reports live owners.

- `archives create` finds the container's writable layer through the container's own mount table, so it works on VMs whose dockerd runs the containerd image store (Docker 29, where `docker inspect` reports no GraphDriver) as well as on overlay2; the run's snapshot directory is removed from the data disk once the snapshot is deleted.

- The admin start the archive and the migrate perform on a stopped workspace now fails at once, with the connector's recorded error, when the start lands the row back on `stopped` (a failed artifact restore), instead of waiting out the whole transfer timeout.
