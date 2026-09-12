-- Migration 041: generic names for the slice columns that survive the gen-2
-- cutover (imbue-ai/mngr-internal#848). The gen-2 fleet carves raw qemu VMs,
-- not lima ones, so ``bare_metal_servers.lima_service_user`` and
-- ``pool_hosts.lima_instance_name`` / ``lima_disk_name`` are renamed to
-- ``slice_service_user`` / ``slice_instance_name`` / ``slice_disk_name``.
--
-- Additive rename, following 037: the new columns are added and backfilled
-- from the old ones, which stay in place -- and keep being dual-written by the
-- admin tooling -- so checkouts from before the rename still read and write
-- them during the rollout window. New code reads ``COALESCE(new, old)`` so rows
-- written by a pre-rename checkout after this migration are still picked up.
-- The connector only ever reads these columns, so it carries COALESCE reads
-- and no dual write.
-- CLEANUP: drop lima_service_user / lima_instance_name / lima_disk_name (a
-- follow-up migration) and the admin tooling's dual writes plus every COALESCE
-- read once every tier's pool DB has applied this migration and no pre-rename
-- checkout is in use.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/041_slice_column_names.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE bare_metal_servers ADD COLUMN slice_service_user TEXT;
ALTER TABLE pool_hosts ADD COLUMN slice_instance_name TEXT;
ALTER TABLE pool_hosts ADD COLUMN slice_disk_name TEXT;

UPDATE bare_metal_servers SET slice_service_user = lima_service_user;
UPDATE pool_hosts SET slice_instance_name = lima_instance_name, slice_disk_name = lima_disk_name;

COMMIT;
