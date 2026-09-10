-- Migration 040: ``bare_metal_servers.uplink_mbps`` becomes NOT NULL
-- (blueprint/pre-cutover-fleet-fixes).
--
-- The declared uplink rate sizes every gen-2 box's fair-share HTB classes,
-- the collector's egress signal, and the link-speed audit; a NULL silently
-- disabled all three. ``server order`` now derives it from the ordered
-- bandwidth option and ``server register`` requires it, so no new row is
-- ever inserted without one. Every box the fleet has rented so far is on the
-- same 1 Gbps plan, so existing rows backfill to 1000.
--
-- The tier's minds-admin (order / register / bake) and connector must be
-- deployed from the same version as this migration: an older admin checkout
-- inserts rows without ``uplink_mbps`` and fails against the NOT NULL column.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/040_uplink_mbps_not_null.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

UPDATE bare_metal_servers SET uplink_mbps = 1000 WHERE uplink_mbps IS NULL;

ALTER TABLE bare_metal_servers ALTER COLUMN uplink_mbps SET NOT NULL;

COMMIT;
