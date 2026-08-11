-- Migration 021: index shares by workspace_domain.
--
-- The broker's /share/authorize path looks up the active share by
-- workspace_domain on every visit to a shared workspace
-- (find_active_share_by_workspace_domain); without an index that is a
-- sequential scan of the shares table.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/021_shares_workspace_domain_idx.sql

CREATE INDEX IF NOT EXISTS shares_workspace_domain_idx ON shares (workspace_domain);
