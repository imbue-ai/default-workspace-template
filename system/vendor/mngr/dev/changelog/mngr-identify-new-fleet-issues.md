- Added `blueprint/pre-cutover-fleet-fixes/`, the plan for the small pre-cutover fixes (drain kept as a maintenance primitive with `undrain`, mandatory `uplink_mbps`, HTB shaping headroom, region cross-checks, the gen-1 boot disk back to 32 GiB, and the lockdown rollout checklist).

- `blueprint/slice-fleet-cutover/plan-slice-fleet-cutover.md` phase 6 no longer deletes `server drain`, `draining` or `SERVER_STATUS_DRAINING`.

- The `mngr_imbue_cloud` import-layer contract allows `primitives` to import the region map from the connector-mounted `slices.gen2_scripts.regions` leaf.
