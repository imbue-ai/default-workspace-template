-- Migration 032: workspace-keyed shares.
--
-- A share belongs to a workspace, not to the machine it currently runs on.
-- workspace_id records the owning workspace (its system-services agent id);
-- share_label is the random 32-hex leading label of new share domains --
-- minted once at the workspace's first share and persisted, so the shared
-- URL survives unshare/re-share and (in future flows) machine changes, and
-- so CT-logged certificate domains stop publicizing internal ids. Rows from
-- before this migration keep both columns NULL and their host-id-derived
-- domains (grandfathered); creates from new clients backfill workspace_id.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/032_workspace_keyed_shares.sql
--
-- Idempotent: rerunning is a no-op once the columns and index exist.

BEGIN;

ALTER TABLE shares ADD COLUMN IF NOT EXISTS workspace_id TEXT;
ALTER TABLE shares ADD COLUMN IF NOT EXISTS share_label TEXT;

CREATE INDEX IF NOT EXISTS shares_user_workspace_idx ON shares (user_id, workspace_id);

COMMIT;
