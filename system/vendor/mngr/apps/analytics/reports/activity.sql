-- Active accounts by day and signal type, plus a weekly-active rollup.
--
-- "Active" is a query-time decision: every candidate signal is its own
-- signal_type row in gold.activity, so changing the definition means
-- changing the WHERE below, not the pipeline. Signals today:
--   app_open          -- any authenticated request (the desktop app running)
--   share_visit       -- visited someone else's shared workspace
--   workspace_created -- created a workspace
--   signup            -- created the account
-- Explorer in-workspace signals join this table in a later phase; comparing
-- their cohort's app_open-to-real-usage ratio is the basis for fleet-wide
-- extrapolation.

-- Daily actives by signal type.
SELECT day, signal_type, count(DISTINCT account_id) AS active_accounts
FROM metrics.gold.activity
GROUP BY day, signal_type
ORDER BY day DESC, signal_type;

-- Weekly actives under one example definition of "active" (any signal that
-- is not just the app sitting open).
SELECT date_trunc('week', day) AS week, count(DISTINCT account_id) AS active_accounts
FROM metrics.gold.activity
WHERE signal_type != 'app_open'
GROUP BY week
ORDER BY week DESC;
