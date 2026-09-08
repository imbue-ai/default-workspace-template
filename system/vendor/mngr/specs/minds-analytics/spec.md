# Minds analytics

Honest-software analytics for minds: understand product usage without
surveillance. Two deliberately different data classes, one lakehouse, and
analysts who query it with coding agents from their own machines.

Related documents:

- [disclosure.md](./disclosure.md) -- the plain-language answer to "what do
  you collect from explorer workspaces?" (source of truth for future
  accounts-surface copy).
- [redaction-contract.md](./redaction-contract.md) -- the exact per-record
  field dispositions for collected transcripts.

## Principles

1. **Two data classes, two rules.** Server-side data (our product DB, our
   own service logs) describes interactions with our services and exists for
   every account -- we aggregate it, we never expand it with client-side
   tracking. In-workspace data is collected only from imbue-hosted workspaces
   of accounts on the explorer plan: the plan membership *is* the consent.
2. **The user can always see what we take.** Nothing analytics-related ships
   inside the workspace template. Each collection run injects the
   then-current script over SSH, writes it to an auditable path inside the
   workspace before executing it, records the run both server-side and
   inside the workspace, and leaves the script in place afterwards.
3. **Raw content never leaves the workspace.** All redaction (structural
   stripping, secret scanning, PII removal) runs inside the container; the
   runner receives only redacted output and treats it as untrusted input.
4. **Access is revocable and failure is acceptable.** A user can remove our
   key from the workspace's `authorized_keys` files (its sshd reads both
   `~/.ssh/authorized_keys` and `/root/.ssh/authorized_keys`) at any time;
   collection then fails, is recorded, and the workspace is skipped. (Doing
   so may count against future billing caps, handled elsewhere.)
5. **Collect raw once, ask questions forever.** The raw feeds are stable and
   change a few times a year; the questions change weekly and are answered
   downstream in SQL that anyone can iterate without touching workspaces or
   deploying anything.

## Architecture

```
 connector product DB (read-only role) ─┐
 OpenObserve R2 parquet (read-only key) ─┼─> aggregation cron ──> metrics lake
 explorer workspaces (SSH, injected     ─┘        (hourly)         (DuckLake)
   script, redacted output)
                                └────────────────────────────> transcripts lake
                                                                 (DuckLake)
 analysts' machines <── DuckDB + read-only Neon role + read-only R2 token
```

- **Storage** is DuckLake: catalog metadata in Neon Postgres, data as parquet
  in R2. One Neon project per env (`analytics-<env>`) holding three
  databases: `metrics` (DuckLake catalog), `transcripts` (DuckLake catalog),
  and `ops` (plain Postgres tables: job bookkeeping, cursors, audit).
- **Two lakes** because R2 access scoping is per-bucket, not per-prefix:
  `analytics-metrics-<env>` (broader analyst access) and
  `analytics-transcripts-<env>` (named product owners only). Enforcement is
  at the Postgres-role and R2-token layer, not in client code.
- **The Modal app** `analytics-<env>` (cron-only, no web endpoint) runs:
  - `aggregation` (hourly): reads the connector DB and the OpenObserve log
    parquet, rewrites the gold tables in the metrics lake.
  - `lake_maintenance` (daily): flushes inlined data, merges adjacent
    files, expires snapshots past 30 days, cleans up unreferenced files.
  - `collection_poll` (every 15 minutes): enumerates online explorer
    workspaces and collects from each at most hourly.
- **No dashboard service.** Analysts attach both lakes read-only with DuckDB
  from their own machines; curated SQL lives in `apps/analytics/reports/`.

## Identity

- The full SuperTokens user id is the analytics key everywhere. Emails never
  enter either lake; email-to-id resolution stays a connector-DB lookup under
  existing admin controls.
- After account deletion the user id becomes an opaque string with no mapping
  to any person, which is what makes retained aggregates harmless.

## Server-side feeds (phases 1-2)

- **Connector product DB** via a dedicated read-only Postgres role:
  `pool_hosts`, `workspace_records`, `shares`, `share_tunnel_logins`,
  `account_entitlements`, `account_attribution`, `download_events`,
  `device_auth_codes`. These already exist; analytics adds no writes.
- **Access logs**: the shared `modal_app_kit` request-logging middleware
  emits one single-line JSON object per request (`type: "http_request"`),
  including the full authenticated user id when a route resolved one
  (stashed into ASGI scope state by the connector's identity resolution).
  Query strings, bodies, and headers beyond the user agent and the
  `X-Imbue-Client` client id stay excluded.
- **Share visits**: the connector's share-authorize path logs one JSON line
  (`type: "share_visit_authorized"`) with visitor user id, host id, owner
  share label, and workspace domain -- turning the history-destroying
  `share_tunnel_logins` upsert into an append-only record.
- **OpenObserve parquet**: both log lines flow through Modal's OTEL
  integration into the per-tier OpenObserve R2 bucket (zstd parquet).
  The aggregation cron reads that bucket directly with a read-only R2 token
  -- no query API, no tunnel, no second pipeline. OpenObserve retains logs
  90 days; anything worth keeping is aggregated into the lake well inside
  that window. The parquet layout is OpenObserve-internal: re-verify on
  version bumps (the instance follows a replace-not-upgrade lifecycle).
- **LiteLLM is deferred**: cloud-minted keys are effectively unused today.

## Gold tables (metrics lake)

- `activity(account_id, day, signal_type, count)` -- every candidate signal
  is its own row; "active" is defined at query time. Signals include
  app-open (authenticated sync requests), workspace create/start/stop,
  share enablement, share visits (as visitor), downloads, and signups.
  Explorer in-workspace signals (phases 3-5) join the same table, so
  explorer data calibrates extrapolation from app-open to true usage.
- `accounts(account_id, plan, created_day, ...)` -- the account dimension.
- `funnel_daily(day, downloads, signups, first_workspaces, ...)` -- from
  `download_events` / `account_attribution` / workspace records.
- `pipeline_health(job, last_success_at, last_run_at, consecutive_failures,
  last_duration_seconds, ...)` -- per-cron staleness and duration, fed from
  the ops DB's `job_runs` table. Each cron records its runtime and warns
  when it approaches its budget, so "the loop is getting too slow" is a
  metric before it is an outage.

Gold tables are rewritten idempotently each run over a trailing window, so a
missed hour heals itself.

## In-workspace collection (phases 3-5)

- Poll every 15 minutes (per-tier configurable); collect from a given online
  workspace at most hourly; ~4 workspaces in parallel under a per-workspace
  timeout; a ~256 MB per-run input budget drains backfills incrementally.
- Consent = explorer-plan membership, diffed into an ops `consent_ledger`.
  Leaving the plan stops collection and deletes nothing.
- Container feeds via the container's sshd with the pool key; VM-level
  latchkey signals via the VM-root hop where a VM-side gateway exists. Each
  hop is independently revocable.
- Feeds: redacted common transcripts, `client_activity` events,
  service/server registration events, git `--numstat` history, and a
  workspace-state snapshot. Cursors are runner-owned; output is one
  multiplexed JSONL stream ending in a `run_summary` line; the runner
  validates and size-caps everything and writes straight into the lakes.
- Raw rows land as typed envelope columns plus a JSON payload column, so
  workspace-side schema drift never breaks collection.
- The collection function never logs payload content (its own logs flow to
  OpenObserve); unexpected exceptions go to Bugsink once the Modal-app
  wiring lands.

## Retention and deletion

- **Snapshot expiry (both lakes): 30 days.** This is the undelete window and
  the physical-deletion bound, not data retention. After a DELETE, rows are
  unqueryable immediately and physically gone once covering snapshots expire
  and cleanup runs -- within 30 days.
- **Row retention: none in v1.** Rows live until the account-deletion path
  removes them; per-table windows come later once real usage is visible.
- **Plan departure deletes nothing**; it only stops collection.
- **Account deletion** removes the account's transcript-lake content and
  writes a deletion-event fact; metrics-lake raw and gold rows survive keyed
  by the orphaned opaque id (so "how many users deleted their accounts"
  stays answerable). `scripts/delete_accounts.py` calls the same path.

## Deployment

- `apps/analytics` deploys as Modal app `analytics-<env>` through
  `minds-admin env deploy`, gated per env: the tier's `deploy.toml` `[analytics]`
  block is the default (off for every tier until bringup), and dynamic dev
  envs override it with a sticky `--with-analytics` / `--without-analytics`
  flag persisted in the env's local state.
- When enabled, the deploy pushes the `analytics-<tier>-<deploy_id>` Modal
  Secret from the tier's Vault entry (schema: `.minds/template/analytics.sh`),
  runs `apps/analytics/migrations/` against the ops DB via the
  schema_migrations runner, and `modal deploy`s the app. The app is
  cron-only, so there is no health-check URL, no client.toml entry, and no
  custom domain.
- Resource provisioning follows the env's resource-ownership model. Dev envs
  (``creates_resources`` tiers) auto-provision an isolated per-env stack on
  their first analytics-enabled deploy -- Neon project ``analytics-<env>``,
  per-env buckets and deterministically named tokens, an ``analytics_reader``
  role on the env's own connector DB -- persisted in the env's local state
  and torn down by ``env destroy``; no manual steps, and no data shared
  between dev envs (the tier's shared OpenObserve bucket is scoped per env by
  the ``minds_env`` stamp on log lines + ``ANALYTICS_LOGS_ENV_FILTER``).
  Shared tiers (staging / production) are provisioned once per tier by the
  operator bringup runbook (`apps/analytics/docs/bringup.md`), following the
  OpenObserve precedent, with the values recorded in the per-tier Vault
  entry.

## Analyst access

- Metrics lake: any analyst gets a read-only Postgres role on the `metrics`
  catalog plus a read-only R2 token on the metrics bucket.
- Transcripts lake: a named list of product owners; credentials are minted
  by hand. Reader access is a judgment call informed by eyeballing early
  redaction output (no formal QA gate).
- `apps/analytics/reports/README.md` carries the attach snippet and the
  credential-minting runbook; `reports/*.sql` are the worked examples
  (`activity`, `pipeline_health`, `funnel`).

## Open questions

- Extrapolation methodology: how explorer in-workspace signals calibrate
  fleet-wide DAU from app-open signals (analyst iteration in `reports/`).
- Per-table retention windows (v1 has none; decide once usage is visible).
- Connectors-feed details as the latchkey VM-side layout settles.
- Dashboard hosting (unhosted DuckDB/agents for now; revisit when a
  non-technical audience appears).
- LiteLLM spend integration (deferred until cloud-minted keys are used).
- Accounts-surface disclosure copy (explicitly out of scope for these PRs).
- Per-feed collection backoff tuning once real fleet timing data exists.
