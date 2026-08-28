-- Migration 001: in-workspace collection bookkeeping and the deletion ledger.
--
-- Five tables behind the explorer-workspace collection loop (phases 3-5 of
-- specs/minds-analytics/spec.md):
--
--  * consent_ledger      -- current explorer-plan membership per account, as
--                           diffed from the connector DB each poll. Leaving
--                           the plan flips is_consenting off and deletes
--                           nothing.
--  * collection_cursors  -- runner-owned per-(host, source) cursors handed to
--                           the injected script; advanced only after a lake
--                           batch commits, so a lost update merely causes a
--                           re-collection deduped by event_id downstream.
--  * collection_host_keys -- last-seen sshd host key per (host, endpoint).
--                           Adoption rotates workspace host keys to
--                           user-generated ones the server never learns, so
--                           the runner records what it saw and flags changes
--                           instead of pinning bake-time keys.
--  * collection_runs     -- append-only audit of every collection attempt
--                           (including refused hops), mirrored by the
--                           in-workspace data/.imbue/analytics/collections.jsonl.
--  * deletion_events     -- one row per account-deletion request handled by
--                           the analytics deletion path (transcript-lake
--                           content removed; metrics aggregates survive).
--
-- Applied automatically by the schema_migrations runner at `minds env
-- deploy` against the analytics ops database when analytics is enabled for
-- the env. Do NOT apply manually: per the runner's convention this file
-- carries no IF NOT EXISTS guards, so a manual apply is not recorded in
-- schema_migrations and the runner's subsequent replay would fail.

CREATE TABLE consent_ledger (
    account_id TEXT PRIMARY KEY,
    is_consenting BOOLEAN NOT NULL,
    first_consented_at TIMESTAMPTZ NOT NULL,
    last_changed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE collection_cursors (
    host_id TEXT NOT NULL,
    source TEXT NOT NULL,
    cursor TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (host_id, source)
);

CREATE TABLE collection_host_keys (
    host_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    host_public_key TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (host_id, endpoint)
);

CREATE TABLE collection_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    outcome TEXT NOT NULL,
    script_version TEXT NOT NULL,
    metrics_rows BIGINT NOT NULL DEFAULT 0,
    transcript_rows BIGINT NOT NULL DEFAULT 0,
    dropped_lines BIGINT NOT NULL DEFAULT 0,
    stdout_bytes BIGINT NOT NULL DEFAULT 0,
    is_host_key_changed BOOLEAN NOT NULL DEFAULT FALSE,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX collection_runs_host_id_started_at_idx ON collection_runs (host_id, started_at);

CREATE TABLE deletion_events (
    id BIGSERIAL PRIMARY KEY,
    account_id TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    transcript_rows_deleted BIGINT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
