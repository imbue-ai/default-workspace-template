-- Migration 044: the ``retired`` stop kind (specs/workspace-stop-kinds.md).
--
-- A retired workspace can never be started again, by anyone: its version
-- cannot run on the gen-2 slice fleet and its data has been archived for its
-- owner to download (``minds-admin archives``). The owner's start answers a
-- structured 409 ``workspace_retired``, and the operator start refuses it too
-- (``minds-admin workspaces set-stop-kind <id> idle`` is the deliberate way
-- back). The kind is stamped by ``minds-admin workspaces retire``.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/044_workspace_stop_kind_retired.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

ALTER TABLE pool_hosts DROP CONSTRAINT pool_hosts_stop_kind_check;
ALTER TABLE pool_hosts ADD CONSTRAINT pool_hosts_stop_kind_check
    CHECK (stop_kind IN ('owner', 'maintenance', 'idle', 'suspension', 'retired'));

COMMIT;
