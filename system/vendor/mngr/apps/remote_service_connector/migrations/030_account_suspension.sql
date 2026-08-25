-- Migration 030: reversible account suspension.
--
-- ``account_entitlements`` gains the suspension flag pair. Orthogonal to the
-- plan/quota columns on purpose: suspend/unsuspend never touches entitlement
-- values, so lifting a suspension restores the account exactly (operator
-- bumps included). NULL suspended_at = not suspended.
--
-- ``r2_keys.suspension_access`` records what the suspend fan-out did to each
-- bucket key ('read' = token policies flipped read-only, 'disabled' = token
-- status disabled). Distinct from the storage-quota sweep's
-- ``enforced_access`` so the sweep can never mistake suspension enforcement
-- for its own and "restore" a suspended key.
--
-- The shares table needs no change: suspension flips ``shares.state`` to the
-- new 'suspended' value (state is an unconstrained TEXT column) while
-- keeping the relay-token rows, so unsuspension is self-healing.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/030_account_suspension.sql
--
-- NOT idempotent on its own: the schema_migrations runner
-- (apps/minds_admin/imbue/minds_admin/envs/migrations.py) records this
-- filename once applied and never re-runs it, so this migration deliberately
-- omits ``IF NOT EXISTS`` guards.

BEGIN;

ALTER TABLE account_entitlements ADD COLUMN suspended_at TIMESTAMPTZ;
ALTER TABLE account_entitlements ADD COLUMN suspended_reason TEXT;

ALTER TABLE r2_keys ADD COLUMN suspension_access TEXT
    CHECK (suspension_access IS NULL OR suspension_access IN ('read', 'disabled'));

COMMIT;
