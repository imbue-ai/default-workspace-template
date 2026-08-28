# analytics

Minds analytics: honest-software product metrics without surveillance. This
app aggregates data we already hold server-side into DuckLake lakehouses that
analysts query with DuckDB from their own machines, and runs the audited
collection loop over consenting explorer-plan workspaces.

The full design lives in [specs/minds-analytics/spec.md](../../specs/minds-analytics/spec.md)
(with the user-facing [disclosure](../../specs/minds-analytics/disclosure.md)
and the [transcript redaction contract](../../specs/minds-analytics/redaction-contract.md)).

## What it does

A cron-only Modal app (`analytics-<env>`, no web endpoint):

- `aggregation` (hourly): one DuckDB session attaches both DuckLakes (Neon
  catalogs + R2 parquet), the connector's product DB (read-only), the
  analytics ops DB, and views over the tier's OpenObserve log parquet -- then
  rewrites the gold tables: `activity` (per-account per-day signal counts,
  server-side and in-workspace signals alike; "active" is defined at query
  time), `accounts`, `funnel_daily`, `pipeline_health`,
  `transcript_daily`/`transcript_tools_daily` (derived from the transcripts
  lake without touching workspaces), and `collection_health`.
- `collection_poll` (every 15 minutes): syncs explorer-plan membership into
  the ops consent ledger, enumerates online explorer workspaces, and
  collects from each at most hourly -- injecting the current collection
  script into `data/.imbue/analytics/` over SSH, running it there (ALL
  transcript redaction happens inside the workspace), validating its stdout
  as untrusted input, and writing the rows straight into the lakes. Every
  attempt (refused hops included) lands in the `collection_runs` audit and
  in the workspace's own `collections.jsonl`.
- `lake_maintenance` (daily): flushes DuckLake inlined data, merges small
  parquet files, expires snapshots past 30 days (the undelete window and the
  physical-deletion bound), and cleans up unreferenced files -- on both
  lakes, which is also what physically removes deleted transcript rows.

## Code layout

The service lives in `imbue/analytics/`:

- `app.py` -- the Modal deployment entrypoint, and nothing else: image,
  `modal.App`, secrets, function definitions. Deployed by file path; the
  shipped modules may never import it.
- `jobs.py` -- the cron job bodies: session assembly, job-run bookkeeping,
  duration warnings.
- `aggregation.py` -- the gold-table SQL (windowed, idempotent) and the run
  function, including the transcript-metrics derivation.
- `collection.py` -- the collection runner: SSH hops with the pool key,
  script injection, untrusted-output validation, lake writes, runner-owned
  cursors, per-attempt audit rows.
- `consent.py` -- explorer-plan consent sync and online-workspace
  enumeration against the connector DB.
- `protocol.py` -- validation of the injected script's multiplexed JSONL
  stdout (size caps, envelope shape, run summary).
- `injected/` -- the collection script itself (`collect.py` plus its
  stdlib-only modules), written into every collected workspace before it
  runs; its dependencies resolve from its own PEP 723 header, never from
  the image. Implements specs/minds-analytics/redaction-contract.md.
- `deletion.py` -- the account-deletion path (transcript-lake DELETE plus
  the deletion_events fact row), called by `scripts/delete_accounts.py`.
- `lake.py` -- DuckDB session helpers: DuckLake attach, Postgres attaches,
  R2 secrets, the raw landing tables, lake maintenance.
- `log_views.py` -- the `logs` schema: typed views parsing our structured
  JSON log lines out of OpenObserve's parquet.
- `settings.py` -- environment-derived configuration (the
  `analytics-<tier>-<deploy_id>` Modal Secret; schema in
  `.minds/template/analytics.sh`).
- `ops_db.py` -- psycopg2 access to the ops database (job_runs, consent
  ledger, cursors, host keys, collection audit, deletion events).
- `deploy_constants.py` -- the image's allowed third-party import roots.

The container receives only these modules plus `imbue.modal_app_kit` --
nothing else from the monorepo exists at runtime, so shipped modules must not
import anything else from it. The rules (and why they exist) are documented
in [libs/modal_app_kit/README.md](../../libs/modal_app_kit/README.md) and
enforced by `test_project_ratchets.py`.

Around the package:

- `migrations/` -- ops-database schema, applied by the deploy flow's
  schema_migrations runner.
- `reports/` -- analyst-owned worked-example SQL (`activity`,
  `pipeline_health`, `funnel`) plus the attach snippet and
  credential-minting runbook in its README. No deploy dependency.
- `dashboards/` -- a local Evidence.dev dashboarding prototype charting one
  dev env's bare-metal box OTel host metrics (extract script plus Evidence
  project; see its README). No deploy dependency.
- `docs/bringup.md` -- the once-per-tier operator runbook (Neon project, R2
  buckets and tokens, read-only connector role, Vault entry).

## Storage layout

One Neon project per env (`analytics-<env>`) holding three databases:

- `metrics` -- DuckLake catalog for the metrics lake (data:
  `analytics-metrics-<env>` R2 bucket). Broader analyst access.
- `transcripts` -- DuckLake catalog for the transcript lake (data:
  `analytics-transcripts-<env>` R2 bucket). Named product owners only.
- `ops` -- plain Postgres tables: `job_runs`, `consent_ledger`,
  `collection_cursors`, `collection_host_keys`, the `collection_runs`
  audit, and `deletion_events`.

Two lakes because R2 access scoping is per-bucket: sensitivity separation is
enforced by Postgres roles and R2 tokens, never by client code.

## Deployment

Deployment is opt-in per env: the tier `deploy.toml`'s `[analytics]` block is
the default (off everywhere until bringup), and dynamic dev envs override it
with the sticky `minds-admin env deploy --with-analytics` / `--without-analytics`
flag. When enabled, `minds-admin env deploy` pushes the `analytics-<tier>-<id>`
Modal Secret, runs `migrations/` against the ops database, and `modal deploy`s
this app.

Where the resources come from depends on the tier. Dev envs auto-provision an
isolated per-env stack on their first analytics-enabled deploy (Neon project
`analytics-<env>`, per-env buckets and tokens, a reader role on the env's own
connector DB; persisted in the env's local state, torn down by `env destroy`)
-- no manual steps. Shared tiers (staging / production) are provisioned once
by hand per [docs/bringup.md](./docs/bringup.md), recorded in the per-tier
Vault entry. Dev envs share the tier's OpenObserve bucket but scope their log
views to their own lines via the `minds_env` stamp
(`ANALYTICS_LOGS_ENV_FILTER`).
