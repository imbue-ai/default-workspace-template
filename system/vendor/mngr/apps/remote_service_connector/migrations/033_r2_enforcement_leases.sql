-- Migration 033: R2 enforcement leases + in-flight ('pending') key markers.
--
-- ``r2_enforcement_leases`` replaces the per-owner ``pg_advisory_xact_lock``
-- that serialized the storage-quota sweep, cleanup grants, rechecks, and
-- account suspension for one owner. The advisory lock had to keep its
-- transaction (and its pooled Neon connection) open across the critical
-- section's Cloudflare calls; a lease row is claimed, renewed, and released
-- in short single-statement transactions instead, so no connection is held
-- across external work, and a dropped connection degrades into a bounded,
-- observable expiry rather than a silent unlock. All expiry comparisons run
-- in SQL against NOW() so client clocks never participate.
--
-- The ``r2_keys`` CHECK constraints gain write-ahead markers for in-flight
-- Cloudflare token transitions: ``enforced_access = 'pending'`` and
-- ``suspension_access = 'pending_read' / 'pending_disabled'`` mean "a
-- transition was started but not confirmed -- the live token policy is
-- untrusted, re-assert before believing anything". The markers are written
-- BEFORE the Cloudflare call, so a crash between the Cloudflare write and
-- the settling DB write leaves a recorded-unknown row instead of a
-- confidently-wrong one.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/033_r2_enforcement_leases.sql
--
-- NOT idempotent on its own: the schema_migrations runner
-- (apps/minds_admin/imbue/minds_admin/envs/migrations.py) records this
-- filename once applied and never re-runs it, so this migration deliberately
-- omits ``IF NOT EXISTS`` guards.

BEGIN;

CREATE TABLE r2_enforcement_leases (
    owner_user_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

-- The constraint names are the Postgres defaults for the inline column
-- CHECKs added by migrations 014 and 030 (<table>_<column>_check); a drift
-- would fail this migration loudly rather than silently skipping the widen.
ALTER TABLE r2_keys DROP CONSTRAINT r2_keys_enforced_access_check;
ALTER TABLE r2_keys ADD CONSTRAINT r2_keys_enforced_access_check
    CHECK (enforced_access IS NULL OR enforced_access IN ('read', 'readwrite', 'pending'));

ALTER TABLE r2_keys DROP CONSTRAINT r2_keys_suspension_access_check;
ALTER TABLE r2_keys ADD CONSTRAINT r2_keys_suspension_access_check
    CHECK (suspension_access IS NULL OR suspension_access IN ('read', 'disabled', 'pending_read', 'pending_disabled'));

COMMIT;
