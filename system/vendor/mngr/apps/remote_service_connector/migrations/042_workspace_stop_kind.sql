-- Migration 042: ``pool_hosts.stop_kind`` -- why a workspace was stopped, and
-- so who may start it again (specs/workspace-stop-kinds.md).
--
-- Every stop transition stamps one of ``owner`` (the user's own stop, from any
-- of their devices), ``maintenance`` (an operator hold, e.g. the gen-1 -> gen-2
-- migration; only an operator start brings it back), ``idle`` (an operator
-- stop to free capacity, e.g. ``server drain``; the user may start it) or
-- ``suspension`` (the account suspend fan-out; rewritten to ``idle`` when the
-- account is unsuspended). Every start clears it. NULL is a row stopped before
-- this column existed and reads as ``owner``, so no backfill is needed.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/042_workspace_stop_kind.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN stop_kind TEXT
    CHECK (stop_kind IN ('owner', 'maintenance', 'idle', 'suspension'));

COMMIT;
