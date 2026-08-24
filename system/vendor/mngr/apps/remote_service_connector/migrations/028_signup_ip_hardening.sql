-- Migration 028: IP-based signup hardening (velocity counters + reputation cache).
--
-- signup_attempts records one row per gated account-creation attempt on the
-- hosted accounts surface (password form and Google OAuth alike), carrying the
-- trusted client IP, its aggregation subnet (/24 for v4, /48 for v6), the IP
-- reputation verdict, and the gate's outcome. The same rows are both the
-- velocity-limit counters (count per IP per hour, per subnet per day) and the
-- real-time abuse-visibility record the 2026-08 signup-spam incident lacked.
-- Rows are pruned opportunistically on insert after the retention window.
--
-- ip_reputation_cache holds the last provider lookup per IP so repeat signups
-- from one IP cost one upstream request per TTL window; it is shared by all
-- connector containers (which have no shared memory). fetched_at doubles as
-- the daily lookup-budget counter: each live lookup upserts it, so counting
-- recent rows bounds spend on the reputation provider. Rows past the cache
-- retention (older than both the TTL and the budget window) are pruned
-- opportunistically on insert.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/028_signup_ip_hardening.sql

CREATE TABLE IF NOT EXISTS signup_attempts (
    id BIGSERIAL PRIMARY KEY,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_ip INET,
    subnet CIDR,
    email TEXT,
    signup_method TEXT NOT NULL,
    verdict TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reputation JSONB
);

CREATE INDEX IF NOT EXISTS signup_attempts_ip_time_idx ON signup_attempts (client_ip, attempted_at);
CREATE INDEX IF NOT EXISTS signup_attempts_subnet_time_idx ON signup_attempts (subnet, attempted_at);
CREATE INDEX IF NOT EXISTS signup_attempts_attempted_at_idx ON signup_attempts (attempted_at);

CREATE TABLE IF NOT EXISTS ip_reputation_cache (
    ip INET PRIMARY KEY,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vpn BOOLEAN NOT NULL,
    proxy BOOLEAN NOT NULL,
    tor BOOLEAN NOT NULL,
    relay BOOLEAN NOT NULL,
    hosting BOOLEAN NOT NULL,
    service TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ip_reputation_cache_fetched_at_idx ON ip_reputation_cache (fetched_at);
