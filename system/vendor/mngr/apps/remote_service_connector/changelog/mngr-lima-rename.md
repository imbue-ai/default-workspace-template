Generic names for the slice columns that survive the gen-2 cutover (imbue-ai/mngr-internal#848).

- Migration 041 adds `bare_metal_servers.slice_service_user` and `pool_hosts.slice_instance_name` / `slice_disk_name`, backfilled from the `lima_*` columns, which stay in place (additive, like 037). The connector only reads these columns, so its reads become `COALESCE(new, old)` under `CLEANUP:` markers; there is no connector dual write.

- The `WorkspaceRow` / `BoxRow` / release-projection fields carry the new names (`slice_instance_name`, `slice_disk_name`, `slice_service_user`).
