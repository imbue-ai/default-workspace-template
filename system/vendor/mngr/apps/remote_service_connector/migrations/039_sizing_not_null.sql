-- Migration 039: the machine sizing columns become NOT NULL (specs/slice-fleet;
-- blueprint/slice-fleet-cutover phase 2).
--
-- Every ``pool_hosts`` row now records its machine's size; the connector no
-- longer carries NULL-tolerant fallbacks (the pre-sizing "count at the default
-- size" rules and the lazy stop-time ``disk_gb`` backfill).
--
-- ``memory_units`` backfills from the bake-stamped ``memory_gb`` attribute
-- (the uniform fleet advertised exactly its unit count in GiB), else the
-- default machine size.
--
-- ``disk_gb`` on a gen-2 row backfills from the artifact manifest's measured
-- data-disk size when one was uploaded, else the default carve size. On a
-- gen-1 row it is the size the machine's data disk has AFTER the gen-2
-- cutover, which is what ``machines show`` and the disk quota should count
-- against: the gen-1 data disk (the box's disk budget split across its slots,
-- minus the 32 GiB boot disk every deployed gen-1 slice was carved with) plus
-- the 16 GiB gen-2 data-disk base -- the cutover grows the transplanted disk
-- by exactly that base, so the machine keeps its home capacity. Mirrors
-- ``compute_gen1_migrated_data_disk_gib`` in ``mngr_imbue_cloud``; a gen-1 row
-- with no box record falls back to the default.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/039_sizing_not_null.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

UPDATE pool_hosts
    SET memory_units = CASE
        WHEN attributes->>'memory_gb' ~ '^[0-9]+$' THEN (attributes->>'memory_gb')::INTEGER
        ELSE 8
    END
    WHERE memory_units IS NULL;

UPDATE pool_hosts
    SET disk_gb = CASE
        WHEN artifact_manifest->>'datadisk_virtual_bytes' ~ '^[0-9]+$'
            THEN GREATEST(1, ((artifact_manifest->>'datadisk_virtual_bytes')::BIGINT / 1073741824))::INTEGER
        ELSE 44
    END
    WHERE disk_gb IS NULL AND box_generation >= 2;

UPDATE pool_hosts AS p
    SET disk_gb = (
        (s.disk_gb - GREATEST(20, CEIL(s.disk_gb * 0.10))::INTEGER) / s.slot_count - 32
    ) + 16
    FROM bare_metal_servers AS s
    WHERE p.bare_metal_server_id = s.id
        AND p.disk_gb IS NULL
        AND p.box_generation < 2
        AND s.disk_gb IS NOT NULL
        AND s.slot_count > 0;

UPDATE pool_hosts SET disk_gb = 44 WHERE disk_gb IS NULL;

ALTER TABLE pool_hosts ALTER COLUMN memory_units SET NOT NULL;
ALTER TABLE pool_hosts ALTER COLUMN disk_gb SET NOT NULL;

COMMIT;
