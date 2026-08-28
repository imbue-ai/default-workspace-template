-- Migration: workspace transition ownership + failure accounting.
--
-- Stop/start supervisors are now fenced by an ownership token: whoever
-- legitimately begins (or takes over) a transition mints a fresh
-- ``transition_id`` under the same CAS that sets the status, and every write
-- a supervisor makes is guarded on it. A superseded or replaced supervisor's
-- writes hit zero rows, so a stale driver can no longer stamp errors (or any
-- other state) onto a row it does not own.
--
-- ``transition_failure_count`` counts consecutive failed drives of the same
-- transition. It backs the watchdog's re-drive backoff and its ops-alert
-- escalation, and resets on success and on every fresh user request.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/029_transition_ownership.sql

BEGIN;

ALTER TABLE pool_hosts ADD COLUMN transition_id UUID;
ALTER TABLE pool_hosts ADD COLUMN transition_failure_count INTEGER NOT NULL DEFAULT 0;

COMMIT;
