-- Migration: workspace stop/start lifecycle columns.
--
-- A leased slice workspace can now be stopped (its lima VM halted and its
-- disks uploaded to the tier's object-storage bucket, freeing the bare-metal
-- slot) and started again later (disks downloaded onto any same-region box
-- with a free slot). The pool_hosts row is the stable identity across the
-- whole lifecycle; only its status and placement columns change:
--
--   leased -> stopping -> stopped -> starting -> leased
--
-- ``stopping`` = VM halted, upload in flight, slot still held.
-- ``stopped``  = artifact verified in object storage, slot freed, placement
--                columns NULL.
-- ``starting`` = a start claimed the row; a supervisor is restoring it
--                (in place during the retention window, or via download).
-- ``crashed``  = an operator abandoned the row (permanently dead box); the
--                user recovers from the workspace's restic backup.
--
-- Placement columns become nullable because a ``stopped`` row has no box:
-- its VM exists only as encrypted objects in the bucket.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/024_workspace_stop_start.sql

BEGIN;

ALTER TABLE pool_hosts ALTER COLUMN vps_address DROP NOT NULL;
ALTER TABLE pool_hosts ALTER COLUMN ssh_port DROP NOT NULL;
ALTER TABLE pool_hosts ALTER COLUMN container_ssh_port DROP NOT NULL;

-- When the user requested the stop (base of the local-retention window).
ALTER TABLE pool_hosts ADD COLUMN stop_requested_at TIMESTAMPTZ;
-- When the row reached ``stopped`` (upload verified, slot freed).
ALTER TABLE pool_hosts ADD COLUMN stopped_at TIMESTAMPTZ;
-- The uploaded artifact: object keys, ciphertext sha256s + sizes, the age
-- recipient, the original host ports (for the port rewrite at start), the
-- lima version, and the generation number.
ALTER TABLE pool_hosts ADD COLUMN artifact_manifest JSONB;
-- The per-stop age identity, wrapped by the tier KEK (base64 nonce+ciphertext).
ALTER TABLE pool_hosts ADD COLUMN wrapped_dek TEXT;
-- Monotonic artifact generation; each re-stop uploads gen N+1 and deletes gen N.
ALTER TABLE pool_hosts ADD COLUMN artifact_generation INTEGER NOT NULL DEFAULT 0;
-- Supervisor liveness stamp; the watchdog cron re-spawns a supervisor for any
-- in-flight (stopping/starting) row whose heartbeat has gone stale.
ALTER TABLE pool_hosts ADD COLUMN transition_heartbeat_at TIMESTAMPTZ;
-- Last transition failure, surfaced to the client (cleared on success).
ALTER TABLE pool_hosts ADD COLUMN transition_error TEXT;

-- The total-workspaces quota (running + stopped), enforced alongside
-- max_remote_workspaces (running only). Existing explorer rows get the plan
-- default; existing ally rows get the larger ally default.
ALTER TABLE plans ADD COLUMN max_total_workspaces INTEGER NOT NULL DEFAULT 10;
ALTER TABLE account_entitlements ADD COLUMN max_total_workspaces INTEGER NOT NULL DEFAULT 10;
UPDATE account_entitlements SET max_total_workspaces = 50 WHERE plan_name = 'ally';

COMMIT;
