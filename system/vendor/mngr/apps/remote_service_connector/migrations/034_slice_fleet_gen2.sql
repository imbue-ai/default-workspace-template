-- Migration: slice fleet generation 2 (specs/slice-fleet-gen2).
--
-- ``box_generation`` records which slice-fleet generation a box runs (1 =
-- bookworm + lima/slirp, 2 = trixie + raw qemu with routed-tap networking);
-- every existing box is generation 1. The same value is stamped onto each
-- ``pool_hosts`` row at bake (and, later, restore) so box-side operations
-- dispatch per row without a join.
--
-- ``uplink_mbps`` is the box's DECLARED uplink rate (from its plan, not
-- measured), the source of truth for gen-2 per-slice fair-share bandwidth
-- classes; NULL disables traffic shaping on the box.
--
-- ``wg_address`` is the box's WireGuard overlay IP for operator management
-- access (gen-2 management-plane lockdown; assigned at prep).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/034_slice_fleet_gen2.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE bare_metal_servers ADD COLUMN box_generation INTEGER NOT NULL DEFAULT 1;
ALTER TABLE bare_metal_servers ADD COLUMN uplink_mbps INTEGER;
ALTER TABLE bare_metal_servers ADD COLUMN wg_address TEXT;

ALTER TABLE pool_hosts ADD COLUMN box_generation INTEGER NOT NULL DEFAULT 1;

COMMIT;
