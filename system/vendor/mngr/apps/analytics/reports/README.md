# Analytics reports

Worked-example SQL over the metrics lake. These are the queries analysts (and
their coding agents) copy from; they have no deploy dependency -- edit and run
them freely from your own machine.

## Attaching the lake (read-only)

You need a read-only Postgres role on the tier's `metrics` catalog database
and a read-only R2 token on the tier's `analytics-metrics-<env>` bucket (see
"Minting analyst credentials" below). Then, in DuckDB (`uv run --with duckdb
python`, or the duckdb CLI):

```sql
INSTALL ducklake; LOAD ducklake;
INSTALL postgres; LOAD postgres;
INSTALL httpfs; LOAD httpfs;

CREATE SECRET metrics_bucket (
    TYPE r2,
    KEY_ID '<access key id>',
    SECRET '<secret access key>',
    ACCOUNT_ID '<cloudflare account id>',
    SCOPE 'r2://analytics-metrics-<env>'
);

ATTACH 'ducklake:postgres:<read-only metrics catalog DSN>' AS metrics (READ_ONLY);

SELECT * FROM metrics.gold.activity LIMIT 10;
```

Fetching TIMESTAMPTZ results through the Python client needs `pytz`
installed alongside `duckdb`.

## The reports

- `activity.sql` -- daily/weekly active accounts by signal type; the
  retention cut. "Active" is a query-time decision: every candidate signal
  is its own `signal_type` row, so redefine actives by changing the WHERE.
  Explorer in-workspace signals (`workspace_chat_message`,
  `workspace_git_commit`, `workspace_user_message`) live in the same table.
- `pipeline_health.sql` -- is the pipeline itself alive: per-cron staleness,
  consecutive failures, and last duration vs. its warning threshold.
- `funnel.sql` -- downloads -> signups -> first workspace, daily.

## Data start dates (production)

The sources came online at different times, so a zero before one of these
floors means "not instrumented yet", never "no usage":

- `gold.activity` has no days before **2026-08-19**: the first aggregation
  run's recompute window started there, and earlier days were never
  computed (the aggregation only rewrites its trailing window).
- `app_open` and `share_visit` derive from structured connector log lines
  that began with the 0.4.2 deploy, **2026-08-25 ~17:15 UTC**. Requests
  before that were logged without user attribution.
- `funnel_daily.downloads` begins **2026-08-21**, when the `/download`
  redirect started recording `download_events`.
- `signup` coalesces the static SuperTokens backfill
  (`gold.accounts_signup`, complete back to 2026-05-20) with
  `account_attribution` (written at account creation on every path since
  **2026-08-17**), so signup counts are complete for the product's lifetime.
- In-workspace signals (`workspace_*`) begin with production collection on
  **2026-08-26**; the git and transcript feeds backfill workspace history,
  so their events reach earlier than that.

Operator-suspended accounts remain in the lakes by design; exclude them
from product metrics via `metrics.gold.accounts.is_suspended` (the worked
examples do). The accounts dimension's `signup_at` is the real signup
moment (backfill-coalesced); its `entitlements_created_at` is only the
lazy creation of the plan row and lags the signup for many accounts.

One raw-data quirk to know: `servers`-feed rows in
`metrics.raw.workspace_events` collected from workspaces older than the
template's event-id fix (post-minds-v0.4.2) share a single `event_id`
fleet-wide -- the template derived it from the service name alone, and raw
is append-only, so that history stays. Queries over the `servers` feed must
dedupe scoped by `host_id`, never by bare event id.

Also in `metrics.gold` (no worked example yet): `transcript_daily` and
`transcript_tools_daily` (turns, tool mix, and error rates derived from the
transcripts lake), and `collection_health` (per-workspace collection
staleness and consecutive failures).

## Minting analyst credentials

Credentials are per-person (deliberately -- per-person Postgres roles make
reads auditable) and minted by an operator with:

```bash
eval "$(uv run minds-admin env activate production)"
uv run minds-admin analytics analyst add <name>            # both lakes (the default)
uv run minds-admin analytics analyst add <name> --no-transcripts   # metrics only
uv run minds-admin analytics analyst list
uv run minds-admin analytics analyst remove <name>
```

`add` creates the `analyst_<name>` read-only role on the tier's
`analytics-<env>` Neon catalogs, mints one read-only bucket-scoped R2 token
per lake, and emits a self-documenting credentials TOML (pass `--output` to
write a 0600 file) to hand to the analyst -- its header includes a
copy-pasteable DuckDB quick start with the real values substituted.
Re-running `add` rotates the credentials; `remove` revokes everything.

Under the hood, per lake, that is exactly:

1. A read-only catalog role on the lake's database:
   `CREATE ROLE <role> WITH LOGIN PASSWORD '...';`
   `GRANT CONNECT ON DATABASE <db> TO <role>; GRANT USAGE ON SCHEMA public TO <role>;`
   `GRANT SELECT ON ALL TABLES IN SCHEMA public TO <role>;`
   `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO <role>;`
2. A read-only R2 token scoped to the lake's bucket (permission group
   "Workers R2 Storage Bucket Item Read"). The S3 access key id is the token
   id; the secret is the SHA-256 hex of the token value.

Transcript-lake access rides along by default because the same set of people
currently needs both lakes; `--no-transcripts` is the opt-out if that ever
narrows.
