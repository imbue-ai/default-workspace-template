-- Destroyed-workspace backup retention.
--
-- Adds the destroyed_at tombstone timestamp that the backup reapers age
-- against. The server stamps it whenever a record transitions to
-- state = 'destroyed' and clears it when a record is resurrected to
-- 'active'. Existing destroyed rows are stamped at migration time so
-- pre-feature tombstones get the full retention window as a rollout
-- grace period (rather than being reaped immediately).
--
-- Also adds the orphan-bucket first-seen table: the server reaper records
-- when it first observes a workspace-backup bucket (a `<prefix>--host-<hex>`
-- name) that no workspace record references, and the retention clock for
-- that bucket runs from this stamp.

ALTER TABLE workspace_records ADD COLUMN IF NOT EXISTS destroyed_at TIMESTAMPTZ;

UPDATE workspace_records SET destroyed_at = NOW() WHERE state = 'destroyed' AND destroyed_at IS NULL;

CREATE TABLE IF NOT EXISTS orphan_backup_buckets (
    bucket_name TEXT PRIMARY KEY,
    first_seen_orphaned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
