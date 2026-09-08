-- Migration 031: key workspace records by their workspace id.
--
-- A workspace's durable identity is its system-services agent id (the
-- "workspace id"); the machine it runs on (host_id) is a mutable attribute.
-- This flips the workspace_records primary key from (user_id, host_id) to
-- (user_id, agent_id), demotes host_id to a plain indexed column (the
-- host-keyed compat routes look rows up through it), and drops the
-- active-only partial unique index the primary key now subsumes.
--
-- Duplicate (user_id, agent_id) rows come from the old restore flow: a
-- tombstone on the old host plus at most one ACTIVE row on the new host
-- (the partial index always forbade two ACTIVE rows). They are collapsed
-- with the ACTIVE row always winning; updated_at only breaks ties between
-- same-state rows, because scrub_secrets and late tombstone re-pushes bump
-- updated_at on tombstones too. A collapsed tombstone's backup bucket,
-- if any, becomes an orphan and is aged out by the orphan reaper.
--
-- backup_bucket records the full R2 bucket name holding the workspace's
-- backups (explicit, instead of deriving it from the host id). Written
-- immediately; served on the wire only once the pre-tolerant strict client
-- fleet is out of the support window (see sync.py).
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/031_workspace_keyed_records.sql
--
-- Idempotent: rerunning is a no-op once the key and columns are in place.

BEGIN;

DELETE FROM workspace_records a
    USING workspace_records b
    WHERE a.user_id = b.user_id
      AND a.agent_id = b.agent_id
      AND (
        (a.state = 'destroyed' AND b.state = 'active')
        OR (
          a.state = b.state
          AND (a.updated_at < b.updated_at OR (a.updated_at = b.updated_at AND a.ctid < b.ctid))
        )
      );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.key_column_usage
        WHERE table_name = 'workspace_records'
          AND constraint_name = 'workspace_records_pkey'
          AND column_name = 'agent_id'
    ) THEN
        ALTER TABLE workspace_records DROP CONSTRAINT workspace_records_pkey;
        ALTER TABLE workspace_records ADD PRIMARY KEY (user_id, agent_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS workspace_records_user_host_idx ON workspace_records (user_id, host_id);

DROP INDEX IF EXISTS workspace_records_one_active_per_agent_idx;

ALTER TABLE workspace_records ADD COLUMN IF NOT EXISTS backup_bucket TEXT;

COMMIT;
