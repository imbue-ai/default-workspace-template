-- Migration 026: marketing attribution for account creations and app downloads.
--
-- account_attribution holds exactly one row per account, written once at
-- account creation (never on sign-in) from the imbue_attribution marketing
-- cookie and/or the signup page's own campaign query params. download_events
-- is an append-only log of GET /download hits, giving campaign -> download
-- conversion its denominator; the visitor id (minted by the marketing site's
-- edge function into the cookie) joins the two exactly. Touches are stored as
-- JSONB blobs whose shape is pinned in
-- apps/remote_service_connector/docs/attribution-cookie-contract.md.
--
-- Applied automatically by the schema_migrations runner at `minds env
-- deploy`. Do NOT apply manually: this file (per the runner's convention)
-- carries no IF NOT EXISTS guards, so a manual apply is not recorded in
-- schema_migrations and the runner's subsequent replay would fail.

CREATE TABLE account_attribution (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    visitor_id TEXT,
    first_touch JSONB,
    last_touch JSONB,
    signup_context TEXT NOT NULL,
    signup_method TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX account_attribution_created_at_idx ON account_attribution (created_at);

CREATE TABLE download_events (
    id BIGSERIAL PRIMARY KEY,
    visitor_id TEXT,
    first_touch JSONB,
    last_touch JSONB,
    platform TEXT NOT NULL,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX download_events_created_at_idx ON download_events (created_at);
