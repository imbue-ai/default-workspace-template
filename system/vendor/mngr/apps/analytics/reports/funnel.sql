-- Acquisition funnel by day: downloads -> signups -> first workspaces.
--
-- Downloads come from the marketing site's /download redirect (visitor-id
-- keyed, no account yet); signups from account creation; first_workspaces
-- counts accounts whose first-ever workspace record landed that day.
-- Attribution joins (campaign -> signup) are plain SQL against the
-- connector's account_attribution / download_events tables via the same
-- aggregation source if needed -- this report stays at the daily-count level.

SELECT day, downloads, signups, first_workspaces
FROM metrics.gold.funnel_daily
ORDER BY day DESC
LIMIT 90;

-- Day-7 activation: of each signup cohort, how many showed any non-app_open
-- activity within 7 days.
WITH signup_days AS (
    SELECT account_id, min(day) AS signup_day
    FROM metrics.gold.activity
    WHERE signal_type = 'signup'
    GROUP BY account_id
)
SELECT
    signup_days.signup_day,
    count(*) AS signups,
    count(*) FILTER (
        WHERE EXISTS (
            SELECT 1 FROM metrics.gold.activity AS later
            WHERE later.account_id = signup_days.account_id
              AND later.signal_type NOT IN ('signup', 'app_open')
              AND later.day BETWEEN signup_days.signup_day AND signup_days.signup_day + INTERVAL 7 DAY
        )
    ) AS active_within_7_days
FROM signup_days
GROUP BY signup_days.signup_day
ORDER BY signup_days.signup_day DESC;
