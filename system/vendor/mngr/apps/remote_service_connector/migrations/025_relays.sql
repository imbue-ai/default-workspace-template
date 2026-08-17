-- Migration 025: relay fleet inventory + per-relay tunnel login stamps.
--
-- Multi-relay sharing (blueprint/multi-relay): the relay fleet becomes data
-- instead of the SHARE_RELAY_ENDPOINTS / SHARE_DEFAULT_REGION env vars. Each
-- region runs N relays (2 in phase 1) and every shared workspace tunnels to
-- all of them, so share creation, the gateway assignment endpoint, and the
-- health-driven DNS reconciliation all read this table.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/025_relays.sql

CREATE TABLE IF NOT EXISTS relays (
    -- Opaque relay identity (relay-<hex>); also the suffix of the relay's
    -- rendered frps plugin-auth path, so Login/NewProxy callbacks are
    -- attributable to one relay.
    relay_id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    -- host:port the workspace's frpc dials (typically <ip>:7000).
    tunnel_endpoint TEXT NOT NULL,
    -- Public IPv4: the region wildcard's DNS answer and the healthz probe target.
    ip_address TEXT NOT NULL,
    -- Human-readable OVH instance name (share-relay-<env>-<region>-<n>).
    instance_name TEXT NOT NULL DEFAULT '',
    -- FALSE once retired; retired relays leave assignment, DNS, and auth.
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    -- 'healthy' / 'unhealthy', driven by the relay_health_sweep cron.
    health TEXT NOT NULL DEFAULT 'healthy',
    consecutive_probe_failures INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS relays_region_idx ON relays (region);

-- One row per (share, relay) tunnel Login, so "connected to m of k relays" is
-- answerable. shares.last_tunnel_login_at stays as the coarse max.
CREATE TABLE IF NOT EXISTS share_tunnel_logins (
    host_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    relay_id TEXT NOT NULL,
    last_login_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (host_id, user_id, relay_id)
);
