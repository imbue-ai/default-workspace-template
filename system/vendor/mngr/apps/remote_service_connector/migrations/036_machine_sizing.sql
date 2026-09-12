-- Migration: variable machine sizing (specs/slice-fleet).
--
-- A machine's size is measured in units (1 unit = 1GiB of guest RAM; it also
-- drives vCPUs and fair-share bandwidth proportionally). ``memory_units`` /
-- ``disk_gb`` are the machine's CURRENT size; ``target_memory_units`` /
-- ``target_disk_gb`` hold a pending resize, applied (and cleared) at the
-- machine's next start. All four are nullable: pre-sizing rows have no
-- recorded size until backfilled.
--
-- ``memory_units`` is backfilled from the bake-stamped ``memory_gb``
-- attribute (the uniform fleet advertised exactly its unit count in GiB).
-- ``disk_gb`` stays NULL = "unknown": per-slice disk used to be derived from
-- the box, so no per-machine number exists to backfill; the next stop's
-- upload records the measured qcow2 virtual size (the lazy backfill).
--
-- ``max_active_machine_units`` caps the units summed across a user's RUNNING
-- machines; ``max_total_machine_disk_gb`` caps data-disk GB across running +
-- stopped. Existing per-user entitlement rows get their plan's new defaults
-- (mirroring how migration 024 seeded max_total_workspaces).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/036_machine_sizing.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN memory_units INTEGER;
ALTER TABLE pool_hosts ADD COLUMN target_memory_units INTEGER;
ALTER TABLE pool_hosts ADD COLUMN disk_gb INTEGER;
ALTER TABLE pool_hosts ADD COLUMN target_disk_gb INTEGER;

UPDATE pool_hosts
    SET memory_units = (attributes->>'memory_gb')::INTEGER
    WHERE attributes->>'memory_gb' ~ '^[0-9]+$';

ALTER TABLE plans ADD COLUMN max_active_machine_units INTEGER NOT NULL DEFAULT 8;
ALTER TABLE plans ADD COLUMN max_total_machine_disk_gb INTEGER NOT NULL DEFAULT 140;
ALTER TABLE account_entitlements ADD COLUMN max_active_machine_units INTEGER NOT NULL DEFAULT 8;
ALTER TABLE account_entitlements ADD COLUMN max_total_machine_disk_gb INTEGER NOT NULL DEFAULT 140;
UPDATE account_entitlements SET max_active_machine_units = 16, max_total_machine_disk_gb = 280
    WHERE plan_name = 'explorer';
UPDATE account_entitlements SET max_active_machine_units = 80, max_total_machine_disk_gb = 1400
    WHERE plan_name = 'ally';

COMMIT;
