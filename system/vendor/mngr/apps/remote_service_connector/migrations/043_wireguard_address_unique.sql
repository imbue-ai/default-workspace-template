-- Migration 043: one box per management overlay address.
--
-- ``bare_metal_servers.wireguard_address`` is a gen-2 box's address on the
-- tier's management WireGuard overlay, assigned sequentially at prep. Two
-- concurrent ``minds-admin server setup`` runs once each read the free set
-- before the other had stamped and both took the same address (production,
-- 2026-09-15). The operator tooling now allocates under a transaction-scoped
-- advisory lock; this partial unique index is the schema-level guard behind
-- it, so no writer (the allocation, the CI box import's upsert, a hand edit)
-- can ever leave two rows on one address again.
--
-- Refuses to apply while a duplicate exists: repair it first (restamp one
-- box to a free address and re-prep it so its wg0 follows), checking with
--     SELECT wireguard_address, count(*) FROM bare_metal_servers
--         WHERE wireguard_address IS NOT NULL GROUP BY 1 HAVING count(*) > 1;
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/043_wireguard_address_unique.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

CREATE UNIQUE INDEX bare_metal_servers_wireguard_address_unique
    ON bare_metal_servers (wireguard_address)
    WHERE wireguard_address IS NOT NULL;

COMMIT;
