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

Also in `metrics.gold` (no worked example yet): `transcript_daily` and
`transcript_tools_daily` (turns, tool mix, and error rates derived from the
transcripts lake), and `collection_health` (per-workspace collection
staleness and consecutive failures).

## Minting analyst credentials

Operators mint per-analyst credentials by hand (deliberately -- per-person
Postgres roles make reads auditable):

1. Read-only catalog role, on the tier's `analytics-<env>` Neon project,
   `metrics` database:
   `CREATE ROLE <name> WITH LOGIN PASSWORD '...';`
   `GRANT CONNECT ON DATABASE metrics TO <name>; GRANT USAGE ON SCHEMA public TO <name>;`
   `GRANT SELECT ON ALL TABLES IN SCHEMA public TO <name>;`
   `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO <name>;`
2. Read-only R2 token scoped to the metrics bucket (Cloudflare dashboard or
   API: permission group "Workers R2 Storage Bucket Item Read"). The S3
   access key id is the token id; the secret is the SHA-256 hex of the token
   value.

Transcript-lake access (the `transcripts` catalog + bucket) is restricted to
a named list of product owners; mint the same way, sparingly.
