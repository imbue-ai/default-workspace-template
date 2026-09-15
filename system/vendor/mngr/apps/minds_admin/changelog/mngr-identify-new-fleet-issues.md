Pre-cutover fleet fixes (`blueprint/pre-cutover-fleet-fixes/`).

- `server order` derives the box's uplink rate from the selected public bandwidth option code (`bandwidth-<mbps>-...`; the vRack option is ignored) and stamps it on the new row. When no selected code carries a rate the order is refused with the selected codes listed and the un-checked-out cart deleted; `--uplink-mbps` is the explicit override.

- `server register --uplink-mbps` is required (a positive integer, on `order` too), and `--region` is validated against the known datacenter codes like `order` already did.

- `server list` shows an `UPLINK` column.

- `server undrain --server-id` returns a `draining` box to `ready` (refused from any other status). `server set-status --status draining` is refused with a pointer to `server drain`, whose docstring now describes it as the box-maintenance primitive (repave, reboot, repair, retirement).

- `pool create` refuses a `--region` label that maps to a different datacenter than the target box's row, or a box whose datacenter the region map does not know, before any Vault read or clone. `import-boxes` refuses source rows with an unknown datacenter.

- The gen-2 prep and the box telemetry collector take the declared uplink as a required value (the `0` / `None` fallbacks are gone).
