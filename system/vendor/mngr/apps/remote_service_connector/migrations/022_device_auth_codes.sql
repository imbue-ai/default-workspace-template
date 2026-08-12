-- Migration 022: one-time device authorization codes for the browser login handoff.
--
-- The hosted accounts surface signs the user in inside their browser and then
-- hands the session to the desktop app / CLI via a loopback redirect carrying
-- a short-lived one-time code (PKCE-bound). The code is stored hashed; the
-- exchange endpoint consumes it atomically (single use) and mints a fresh
-- SuperTokens session for the device. Rows are tiny and expire in minutes;
-- consumed/expired rows are deleted opportunistically on each mint.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/022_device_auth_codes.sql

CREATE TABLE IF NOT EXISTS device_auth_codes (
    code_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS device_auth_codes_expires_at_idx ON device_auth_codes (expires_at);
