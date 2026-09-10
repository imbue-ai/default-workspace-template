-- Migration: gen-2 management-plane lockdown, box WireGuard public key
-- (specs/slice-fleet-gen2, phase 3).
--
-- ``wg_public_key`` is the box's WireGuard public key. The matching private
-- key is generated on the box by the gen-2 prep and never leaves it; prep
-- reads the public half back and stamps it here so operator client configs
-- (``minds-admin wg config``) can pin each box peer without touching the box.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/035_wg_public_key.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE bare_metal_servers ADD COLUMN wg_public_key TEXT;

COMMIT;
