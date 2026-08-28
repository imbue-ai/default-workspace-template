Dev envs now auto-provision an isolated per-env analytics stack -- there are no manual per-env analytics steps anymore.

- The first `minds-admin env deploy --with-analytics` of a dev env provisions Neon project `analytics-<env>` (metrics/transcripts/ops databases, direct DSNs), R2 buckets `analytics-{metrics,transcripts}-<env>` with bucket-scoped account tokens (`analytics-<kind>-<env>-rw`), a read-only token on the tier's shared OpenObserve bucket (`analytics-logs-<env>-ro`), and an `analytics_reader` role on the env's own host_pool database. Values persist in the env's local `secrets.toml`; re-deploys reuse them, and a re-provision rotates credentials (delete-by-name then mint) so nothing accumulates.

- The env's analytics Modal Secret is composed from that stack (instead of the per-tier Vault entry, which stays the path for staging/production) and sets `ANALYTICS_LOGS_ENV_FILTER=<env>` so the aggregation only sees the env's own service-log lines (collection tuning keeps the production defaults).

- `minds-admin env destroy` tears the whole stack down (Neon project, emptied buckets, revoked tokens). Token and bucket names are deterministic per env so a future sweep can reap strays without the env's local state.
