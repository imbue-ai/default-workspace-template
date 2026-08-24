-- Is the analytics pipeline itself alive?
--
-- Check this first when any dashboard looks stale. Warning thresholds:
-- aggregation runs hourly (stale past ~2h), lake_maintenance daily (stale
-- past ~26h); last_duration_seconds approaching the cron's warning threshold
-- (300s aggregation / 600s maintenance) means the job is outgrowing its
-- budget and needs attention before it starts timing out.

SELECT
    job_name,
    last_success_at,
    last_run_at,
    consecutive_failures,
    last_duration_seconds,
    now() - last_success_at AS staleness
FROM metrics.gold.pipeline_health
ORDER BY job_name;
