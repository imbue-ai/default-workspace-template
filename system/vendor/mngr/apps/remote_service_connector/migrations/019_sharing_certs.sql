-- Migration 019: sharing follow-ups -- per-user share rows, tunnel liveness, ACME tables.
--
-- 018 keyed shares by host_id alone, which lets one user's (possibly stale or
-- squatted) row permanently block another user from sharing a workspace that
-- legitimately changed hands (pool hosts are re-leased; restored workspaces
-- keep their host ids in backups). Shares are now keyed (host_id, user_id):
-- each user's share of a host id is their own row with its own domain
-- (the user label is part of the hostname, so two users' shares of one host
-- id never collide at the relay). relay_tokens follows the same composite key.
--
-- Both tables shipped in 018 but the feature has never been enabled, so they
-- are empty everywhere and the NOT NULL column additions below are safe.
--
-- Also adds the ACME tables for the connector's DNS-01 CSR-signing endpoint:
-- acme_accounts (one registered account per CA directory, private account key
-- included -- these are operational credentials, not workspace secrets) and
-- issued_certs (cert chains only; workspace private keys never leave the
-- workspace).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/019_sharing_certs.sql

-- Wrapped in a transaction (and every ADD guarded with a prior DROP ... IF
-- EXISTS) so a partial apply rolls back cleanly and the whole script is
-- re-runnable rather than wedging on "constraint already exists".
BEGIN;

ALTER TABLE relay_tokens DROP CONSTRAINT IF EXISTS relay_tokens_host_id_fkey;
-- Drop the composite FK before the primary key it references: on a re-run the
-- FK (added below) exists and would otherwise block the shares_pkey drop.
ALTER TABLE relay_tokens DROP CONSTRAINT IF EXISTS relay_tokens_share_fkey;
ALTER TABLE shares DROP CONSTRAINT IF EXISTS shares_pkey;
ALTER TABLE shares ADD PRIMARY KEY (host_id, user_id);
ALTER TABLE shares ADD COLUMN IF NOT EXISTS last_tunnel_login_at TIMESTAMPTZ;

ALTER TABLE relay_tokens ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL;
ALTER TABLE relay_tokens
    ADD CONSTRAINT relay_tokens_share_fkey
    FOREIGN KEY (host_id, user_id) REFERENCES shares (host_id, user_id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS relay_tokens_share_idx ON relay_tokens (host_id, user_id);

COMMIT;

CREATE TABLE IF NOT EXISTS acme_accounts (
    ca_name TEXT NOT NULL,
    directory_url TEXT NOT NULL,
    -- PEM of the ACME account's private key. Account keys are connector
    -- operational credentials (like the DNS API token), not user data.
    account_key_pem TEXT NOT NULL,
    account_uri TEXT NOT NULL,
    -- External Account Binding key id, for CAs that require EAB (ZeroSSL,
    -- Google Trust Services). The EAB HMAC itself stays in the env secret.
    eab_kid TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ca_name, directory_url)
);

CREATE TABLE IF NOT EXISTS issued_certs (
    cert_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_domain TEXT NOT NULL,
    host_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    ca_name TEXT NOT NULL,
    -- Full PEM chain (leaf first). No private keys: the workspace generates
    -- its key and sends a CSR; only the signed chain comes back here.
    cert_chain_pem TEXT NOT NULL,
    -- JSON array of the SANs on the issued cert.
    sans TEXT NOT NULL,
    not_after TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS issued_certs_workspace_domain_idx ON issued_certs (workspace_domain);
