Pre-cutover fleet fixes (`blueprint/pre-cutover-fleet-fixes/`).

- Migration 040 backfills `bare_metal_servers.uplink_mbps` to 1000 where NULL (the fleet's one plan) and makes the column NOT NULL. Deploy the tier's minds-admin and connector from the same version: an older `server order` / `register` would insert a NULL and fail.

- The restore candidate filter maps a workspace's lease-region label through the plugin's shared `gen2_scripts.regions` map instead of a hand-duplicated copy; the box row's `uplink_mbps` is no longer Optional.
