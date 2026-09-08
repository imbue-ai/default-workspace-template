-- Active accounts by day and signal type, plus a weekly-active rollup.
--
-- "Active" is a query-time decision: every candidate signal is its own
-- signal_type row in gold.activity, so changing the definition means
-- changing the WHERE below, not the pipeline. Signals today:
--   app_open          -- any authenticated request (the desktop app running)
--   share_visit       -- visited someone else's shared workspace
--   share_enabled     -- enabled sharing for one of their own workspaces
--   workspace_created -- created a workspace
--   signup            -- created the account
--   workspace_chat_message / workspace_git_commit / workspace_user_message
--                     -- explorer in-workspace signals from the collected
--                        feeds (workspace_git_commit counts only commits
--                        unique to one workspace, so shared template history
--                        never counts as user activity)
-- Comparing the explorer cohort's app_open-to-real-usage ratio is the basis
-- for fleet-wide extrapolation.
--
-- Operator-suspended accounts stay in the lake by design; product metrics
-- exclude them through the accounts dimension, as below. See the README's
-- "Data start dates" section for when each signal begins.

-- Daily actives by signal type.
SELECT day, signal_type, count(DISTINCT account_id) AS active_accounts
FROM metrics.gold.activity
WHERE account_id NOT IN (SELECT account_id FROM metrics.gold.accounts WHERE is_suspended)
GROUP BY day, signal_type
ORDER BY day DESC, signal_type;

-- Weekly actives under one example definition of "active" (any signal that
-- is not just the app sitting open).
SELECT date_trunc('week', day) AS week, count(DISTINCT account_id) AS active_accounts
FROM metrics.gold.activity
WHERE signal_type != 'app_open'
  AND account_id NOT IN (SELECT account_id FROM metrics.gold.accounts WHERE is_suspended)
GROUP BY week
ORDER BY week DESC;
