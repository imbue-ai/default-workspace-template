Pool leases and workspace records are now kept consistent by the connector (mngr-internal#642):

- `POST /hosts/lease` and `POST /hosts/claim` insert a metadata-only ACTIVE workspace record stub in the same transaction as the lease grant, so a lease without a record never exists; CLI-created workspaces now appear in every signed-in client's list.

- Every release path (owner, operator, failed-claim rollback, sweep) retires the workspace's ACTIVE record in the same transaction as the row's `removing` flip -- tombstoning a client-written record, deleting a never-written lease stub (revision 1, no secrets, no backup bucket) outright -- so an out-of-band `mngr destroy` no longer leaves an active record behind and a create that failed after its lease leaves no ghost in "recently destroyed".

- Tombstone-first: `DELETE /sync/records/...` answers `409 lease_active` while the workspace still holds a pool lease (any lifecycle status), and the backup-retention reaper leaves such tombstones alone until the lease is gone.

- New hourly `lease_record_sweep` cron (`POST /admin/sweep/lease-records` on demand, `?dry_run=1`): releases leases whose record tombstone is older than 6 hours and re-drives rows that have sat in `removing` for as long (a fresh flip is a release still in flight and is left alone); a lease with no record at all is reported (one warning per pass) and never auto-reaped. Per-kind counts emit as `lease_record_drift` metrics.

- New operator endpoint `POST /admin/workspaces/{host_db_id}/release`: the owner's exact release chain regardless of owner, for any lifecycle status including `stopped`.

- The release chain no longer holds a pooled DB connection across its SSH/S3 work, and a `limactl delete` that finds its instance or disk already absent now counts as done (the shell's `limactl: command not found` is not counted as absent), so a release interrupted between the VM teardown and the row delete converges on retry instead of wedging in `removing`.

- The connection pool probes a connection with `SELECT 1` before handing it out when it has sat idle for over 60 seconds (discarding dead ones), so a container waking from a quiet spell no longer answers its first requests with a dropped connection; busy paths pay no extra round trip.
