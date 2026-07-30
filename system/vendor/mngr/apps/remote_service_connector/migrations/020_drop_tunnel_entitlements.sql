-- Migration 020: drop the Cloudflare-tunnel quota entitlements.
--
-- The Cloudflare tunnel/Access sharing stack (the /tunnels/* endpoints) has
-- been removed from the connector in favor of the self-hosted sharing relays
-- (migrations 018/019), so the per-account tunnel quotas no longer gate
-- anything. Drops the two columns from both the git-owned plan definitions
-- and the per-account entitlement rows.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/020_drop_tunnel_entitlements.sql

ALTER TABLE plans DROP COLUMN IF EXISTS max_tunnels;
ALTER TABLE plans DROP COLUMN IF EXISTS max_services_per_tunnel;

ALTER TABLE account_entitlements DROP COLUMN IF EXISTS max_tunnels;
ALTER TABLE account_entitlements DROP COLUMN IF EXISTS max_services_per_tunnel;
