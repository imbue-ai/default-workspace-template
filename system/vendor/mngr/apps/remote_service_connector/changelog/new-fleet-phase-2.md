The in-supervisor gen-1 -> gen-2 conversion is gone (mngr-internal `new-fleet-phase-2`, slice-fleet cutover phase 2): a stopped workspace only ever restores onto a box of its artifact's own generation, so the rebuilt cidata, the strict trixie guest upgrade, the reboot wait and their failure states no longer exist. Cutting a tier over to gen-2 is a one-time operator window (`blueprint/slice-fleet-cutover`), not a per-restore path.

- Migration `039_sizing_not_null.sql`: `pool_hosts.memory_units` and `disk_gb` are backfilled and made NOT NULL. A gen-2 row takes the artifact manifest's measured data-disk size (else 44 GiB); a gen-1 row takes the size its data disk has after the cutover (the lima data disk plus the 16 GiB gen-2 base), so the disk quota and `machines show` count the capacity the machine will have.

- Every NULL-tolerant sizing fallback is deleted: the pre-sizing "count at the default size" `COALESCE` in the quota sums and the eviction planner, the stop-time `disk_gb` backfill and its `machine_disk_backfilled` metric, and the `attributes.memory_gb` mirror the restore/in-place CAS statements maintained. The machine and workspace APIs report `memory_units` / `disk_gb` as required integers.

- The `box_generation` lease request field (the exact-match generation filter) is removed; a lease is placed by region alone.
