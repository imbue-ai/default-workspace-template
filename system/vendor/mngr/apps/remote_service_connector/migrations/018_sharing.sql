-- Migration 018: self-hosted sharing relays (share records + relay tokens).
--
-- Replaces the Cloudflare tunnel/Access sharing model with the self-hosted
-- relay design (blueprint/sharing-redesign): each shared workspace has one
-- share row and one opaque relay token. The connector authorizes an frps
-- Login/NewProxy against the token and the share's active state; authorization
-- of individual recipients (the grants list) lives in the workspace gateway,
-- not here.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/018_sharing.sql

CREATE TABLE IF NOT EXISTS shares (
    host_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    region TEXT NOT NULL,
    -- The workspace's registrable domain: host-<hex>.<user-id>.<region>.<content_domain>.
    workspace_domain TEXT NOT NULL,
    -- 'active' while the share is enabled; 'inactive' after unshare (kept for
    -- audit / fast re-share rather than deleted).
    state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS shares_user_id_idx ON shares (user_id);
CREATE INDEX IF NOT EXISTS shares_state_idx ON shares (state);

CREATE TABLE IF NOT EXISTS relay_tokens (
    -- SHA-256 hex of the opaque relay token; the plaintext is returned to the
    -- workspace once at share-enable and never stored.
    token_hash TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES shares (host_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS relay_tokens_host_id_idx ON relay_tokens (host_id);
