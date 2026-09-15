`new-fleet-base` is the integration base for the in-progress slice-fleet generation-2 program (specs/slice-fleet-gen2 and specs/slice-fleet): the squash of the formerly stacked PRs #571, #573, #574, #581, #609, and #614. It is deployed only to dev canaries and must not be deployed or merged as-is; follow-up PRs stack on it and the whole program lands on `main` as one change.

For this project it carries:

- Migrations 034-037: `box_generation` on boxes and rows, `uplink_mbps`, the WireGuard columns (`wg_address` / `wg_public_key`, renamed to `wireguard_address` / `wireguard_public_key` with the old names dual-written), the machine-sizing columns (`memory_units`, `target_memory_units`, `disk_gb`, `target_disk_gb`) and the two plan quotas.

- Gen-2 box scripts (`box_scripts_gen2.py`: upload / download / restore-reserve / in-place resize / stop / restart / finalize against the `/srv/mngr-slices` layout), with the plugin-mirrored constants and renderers pinned by a drift ratchet; every supervisor, teardown, and reconcile operation dispatches on the row's or box's generation.

- The gen-1 -> gen-2 conversion inside the start supervisor (rebuilt cidata with a placement-keyed instance-id, the strict in-VM bookworm -> trixie dist-upgrade, `attributes.guest_upgrade_failed`), which the follow-up work replaces with a one-time migration; `draining` boxes force restores elsewhere; the Modal Proxy attach on every function (name + environment, forwarded into containers by an inline secret).

- Machine sizing: `POST /machines/{id}/resize` (user and admin), the quota enforcement at lease / resize / start, restart-in-place resize, restore at the target size, two-pass eviction of unleased rows, the measured-size `disk_gb` backfill, the sizing metrics, and the additive wire fields; the optional exact-match `box_generation` lease filter.

The detailed per-phase history is in this directory's `mngr-design-network-observation`, `mngr-slice-fleet-gen2-phase-*`, `mngr-variable-sizing`, `mngr-new-fleet-testing`, `mngr-finish-new-fleet-canary-testing`, `mngr-slice-fleet-canary-followups`, and `mngr-machine-size-display` entries. Merging `main` into the base also routed every gen-2 database access through `db.pooled_db_connection`.

- The gen-2 migrations were renumbered 033-036 -> 034-037 when `main` landed its own `033_r2_enforcement_leases.sql` (the repo-wide unique-number ratchet refuses two `033_*` files; the WireGuard rename migration must still sort after the one that creates the `wg_*` columns, so the whole chain moved). Dev envs that already applied the old filenames (the runner records applied migrations by filename) must have the new names recorded before their next `env deploy`, or the deploy re-applies them and fails on the duplicate columns: `INSERT INTO schema_migrations (version) VALUES ('034_slice_fleet_gen2.sql'), ('035_wg_public_key.sql'), ('036_machine_sizing.sql'), ('037_wireguard_column_names.sql') ON CONFLICT (version) DO NOTHING;`

- The `host_lease_request` metric distinguishes machine-sizing quota refusals: a lease refused by `max_active_machine_units` / `max_total_machine_disk_gb` is tagged `outcome=quota_refused` instead of being misfiled as `injection_failed`.
