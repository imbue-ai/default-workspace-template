Per-env log isolation for dev analytics stacks: the aggregation's log views accept an env filter.

- New optional `ANALYTICS_LOGS_ENV_FILTER` secret value: when set, `logs.http_requests` / `logs.share_visits` include only lines stamped with that `minds_env` value. Dev envs share one per-tier OpenObserve bucket, so their auto-provisioned stacks set it to the env name; shared tiers leave it blank (blank includes every line, stamped or not).

- The bringup runbook now applies to shared tiers only -- dev envs auto-provision their own stack via `minds-admin env deploy --with-analytics` with no manual steps (see apps/minds_admin's entry for this branch).
