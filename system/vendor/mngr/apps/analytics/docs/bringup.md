# Analytics tier bringup (shared tiers only)

Once-per-tier operator runbook standing up the resources the analytics app
needs on the SHARED tiers (staging / production). Follows the OpenObserve
bringup pattern: resources are created by hand, recorded in Vault, and
consumed by `minds-admin env deploy`.

**Dev (and other `creates_resources` tiers) need none of this.** Every dev
env that deploys with `--with-analytics` auto-provisions its own isolated
stack -- Neon project `analytics-<env>`, buckets
`analytics-{metrics,transcripts}-<env>` with scoped tokens
(`analytics-<kind>-<env>-rw` / `analytics-logs-<env>-ro`), and an
`analytics_reader` role on the env's own host_pool DB -- and persists the
values in the env's local `secrets.toml`. `minds-admin env destroy` tears the
stack down again. The only shared piece is the tier's OpenObserve bucket:
each env reads it with its own read-only token and scopes its log views to
its own lines via the `minds_env` stamp (`ANALYTICS_LOGS_ENV_FILTER`).

Prerequisites: Vault access to `secrets/minds/<tier>/`, the tier's Neon org
API token (`neon-admin`), and the tier's Cloudflare account credentials
(`cloudflare`). The collection loop additionally rides the tier's existing
`pool-ssh` Vault entry (the deploy pushes it as its own Modal Secret, which
the `collection_poll` function attaches) -- every real tier already has it;
nothing analytics-specific to provision there.

## 1. Neon project

In the tier's Neon org, create project `analytics-<env>` (same region as the
tier's other projects) with three databases:

- `metrics` -- the metrics DuckLake catalog
- `transcripts` -- the transcript DuckLake catalog
- `ops` -- job bookkeeping, collection cursors, consent ledger, the
  collection audit, and deletion events

Note the **direct** (non `-pooler`) connection URIs -- DuckLake catalogs and
schema migrations both need session-scoped behavior that PgBouncer's
transaction pooling breaks.

## 2. R2 buckets and tokens

In the tier's Cloudflare account:

- Create buckets `analytics-metrics-<env>` and `analytics-transcripts-<env>`.
- Mint one account-owned API token per bucket, scoped to that bucket only,
  permission group "Workers R2 Storage Bucket Item Write" (the app writes
  parquet). The S3 access key id is the token id; the secret access key is
  the SHA-256 hex of the token value.
- Mint one **read-only** token ("Workers R2 Storage Bucket Item Read")
  scoped to the tier's OpenObserve bucket (`minds-observability-<tier>`) --
  the aggregation reads the log parquet directly.

## 3. Read-only role on the connector database

On the tier's connector (host_pool) database:

```sql
CREATE ROLE analytics_reader WITH LOGIN PASSWORD '<generated>';
GRANT CONNECT ON DATABASE <dbname> TO analytics_reader;
GRANT USAGE ON SCHEMA public TO analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analytics_reader;
```

Use the direct (non-pooler) URI for this role in the Vault entry.

## 4. Vault entry

Copy the template, fill it in, push, and shred (the standard flow):

```bash
cp .minds/template/analytics.sh /tmp/analytics-<tier>.sh
$EDITOR /tmp/analytics-<tier>.sh
uv run scripts/push_vault_from_file.py <tier> analytics /tmp/analytics-<tier>.sh
shred -u /tmp/analytics-<tier>.sh
```

Leave `ANALYTICS_LOGS_ENV_FILTER` blank on shared tiers -- the tier's
OpenObserve bucket carries only that tier's lines, so everything (stamped or
not) is included. The optional collection knobs are runtime configuration:
changing them later means re-pushing the entry and redeploying. The poll
cadence itself is deploy-time: export
`ANALYTICS_COLLECTION_POLL_CRON="*/5 * * * *"` before `minds-admin env deploy` to
poll faster than the default `*/15`.

## 5. Enable and deploy

- Shared tiers: flip `[analytics] is_deployed = true` in the tier's
  `deploy.toml` and run the normal `minds-admin env deploy`.
- Dynamic dev envs (no runbook steps needed): `minds-admin env deploy
  --with-analytics` (sticky for that env; `--without-analytics` turns it back
  off). The first enabled deploy auto-provisions the env's own stack (the
  collection tuning keeps the production defaults).

The deploy pushes the `analytics-<tier>-<deploy_id>` Modal Secret, applies
`apps/analytics/migrations/` to the ops database, and deploys the
`analytics-<env>` Modal app.

## 6. Verify

- `modal app list` (tier workspace/env) shows `analytics-<env>`.
- Trigger the aggregation once from the Modal dashboard (or wait for the
  hourly cron), then check `metrics.gold.pipeline_health` via the attach
  snippet in [../reports/README.md](../reports/README.md).
- Re-verify the OpenObserve parquet layout assumption
  (`ANALYTICS_LOGS_PARQUET_GLOB`, body/timestamp columns in
  `imbue/analytics/log_views.py`) whenever the OpenObserve version changes:
  the layout is internal to OpenObserve.
- Verify the collection loop against one real explorer workspace: put a test
  account on the explorer plan (`mngr imbue_cloud admin account set-plan`),
  lease a workspace for it, wait a poll tick, and check
  `ops.collection_runs` (outcome `ok`) plus `data/.imbue/analytics/` inside
  the workspace (the injected script, `collections.jsonl`). The first
  collection of a workspace resolves the script's pinned environment
  (Presidio + the spacy model) via `uv` inside the workspace -- expect that
  run to take a minute or two longer; it is cached afterwards. Validate
  against an ADOPTED and stop/start-cycled workspace too -- adoption rotates
  the workspace host keys, and the audit row's `is_host_key_changed` should
  flag exactly one change, not fail.
