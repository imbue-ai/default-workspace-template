-- Migration 000: job-run bookkeeping for the analytics crons.
--
-- job_runs is the append-only record of every cron execution (aggregation,
-- lake maintenance, and later the collection poll). The aggregation reads it
-- back (via the read-only ``ops`` attach) to build the gold pipeline_health
-- table: per-job staleness, consecutive failures, and last duration.
--
-- Applied automatically by the schema_migrations runner at `minds-admin env
-- deploy` against the analytics ops database when analytics is enabled for
-- the env. Do NOT apply manually: per the runner's convention this file
-- carries no IF NOT EXISTS guards, so a manual apply is not recorded in
-- schema_migrations and the runner's subsequent replay would fail.

CREATE TABLE job_runs (
    id BIGSERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    is_success BOOLEAN NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX job_runs_job_name_started_at_idx ON job_runs (job_name, started_at);
