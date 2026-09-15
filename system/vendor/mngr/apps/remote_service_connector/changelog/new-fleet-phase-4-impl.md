Phase 4 of the slice-fleet cutover: the connector-side pieces the cutover window needs.

- `POST /workspaces/{id}/start` on a gen-1 row parked by the cutover (stopped, no placement, no artifact manifest) answers `409` with code `workspace_migrating` ("this workspace is being migrated to new infrastructure and will come back on its own") instead of trying to restore it. The guard is `# CLEANUP:` marked for phase 6; the admin start endpoint below stays as an operator tool.

- New admin endpoint `POST /admin/workspaces/{host_db_id}/start` (admin key): starts a stopped workspace on the owner's behalf without the owner's quota check; idempotent for `leased`/`starting`, 404 for an unknown row. Exposed as `minds-admin workspaces start`.

- The gen-2 eviction planner and box budgets use the box's recorded `disk_gb` as the storage partition size (minus the 64 GiB gen-2 reserve) now that `server prep` records the measured partition.

- The `host_lease_request` metric distinguishes machine-sizing quota refusals: a lease refused by `max_active_machine_units` / `max_total_machine_disk_gb` is tagged `outcome=quota_refused` instead of being misfiled as `injection_failed`.
