-- The fleet version mix: distinct accounts per client version per hour,
-- from gold.client_versions_hourly (account x hour x raw X-Imbue-Client
-- identifier, e.g. 'minds/0.4.2 imbue-cloud-plugin/0.1.6').
--
-- The desktop client polls sync endpoints about once a minute while it is
-- open, so any hour an install was running is represented. That makes the
-- hourly cut good for watching a staged rollout move: as installs take an
-- update and relaunch, their accounts shift buckets within the hour.
--
-- The identifier is stored verbatim; parse the product version out at query
-- time. '' is the unversioned bucket (clients older than minds 0.4.1, which
-- sent no header) -- see the README's "Data start dates" section. Rows whose
-- identifier has no 'minds/' half (the bare imbue-cloud CLI, the hosted web
-- surface) keep their raw string under minds_version '' here; split them out
-- by imbue_client instead if they matter for the question at hand.

-- Distinct accounts per minds version per hour, most recent 48 hours.
SELECT hour,
       regexp_extract(imbue_client, 'minds/([^ ]+)', 1) AS minds_version,
       count(DISTINCT account_id) AS accounts
FROM metrics.gold.client_versions_hourly
WHERE hour >= now() - INTERVAL 48 HOUR
  AND account_id NOT IN (SELECT account_id FROM metrics.gold.accounts WHERE is_suspended)
GROUP BY hour, minds_version
ORDER BY hour DESC, accounts DESC;

-- The current mix in one row per version: each account counted once, at the
-- version its most recent hour carried, over the trailing 24 hours. Hourly
-- rows cannot rank requests within the hour, so when an account shows more
-- than one identifier in its latest hour (a mid-hour upgrade, or two clients
-- at once) the tie breaks toward the busier identifier.
WITH latest AS (
    SELECT account_id, imbue_client,
           row_number() OVER (
               PARTITION BY account_id ORDER BY hour DESC, request_count DESC, imbue_client DESC
           ) AS recency
    FROM metrics.gold.client_versions_hourly
    WHERE hour >= now() - INTERVAL 24 HOUR
      AND account_id NOT IN (SELECT account_id FROM metrics.gold.accounts WHERE is_suspended)
)
SELECT regexp_extract(imbue_client, 'minds/([^ ]+)', 1) AS minds_version,
       count(*) AS accounts
FROM latest
WHERE recency = 1
GROUP BY minds_version
ORDER BY accounts DESC;
