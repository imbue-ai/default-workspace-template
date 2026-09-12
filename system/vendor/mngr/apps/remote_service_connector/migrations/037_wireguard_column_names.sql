-- Migration: spell out WireGuard in the bare_metal_servers column names
-- (the slice-fleet handoff's wg -> WireGuard conversion; the no-abbreviations
-- style rule applies to identifiers too).
--
-- Additive rename: the new columns are added and backfilled from the old
-- ``wg_address`` / ``wg_public_key``, which stay in place -- and keep being
-- dual-written by the admin tooling -- so checkouts from before the rename
-- still read and write them during the rollout window. New code reads
-- ``COALESCE(new, old)`` so rows written by a pre-rename checkout after this
-- migration are still picked up.
-- CLEANUP: drop wg_address / wg_public_key (a follow-up migration) and the
-- admin tooling's dual writes and COALESCE reads once every tier's pool DB
-- has applied this migration and no pre-rename checkout is in use.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/037_wireguard_column_names.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE bare_metal_servers ADD COLUMN wireguard_address TEXT;
ALTER TABLE bare_metal_servers ADD COLUMN wireguard_public_key TEXT;

UPDATE bare_metal_servers
    SET wireguard_address = wg_address, wireguard_public_key = wg_public_key;

COMMIT;
