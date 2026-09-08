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

Prerequisites: Vault access to `secrets/minds/<tier>/` and the tier's
Cloudflare account credentials (`cloudflare`). No Neon API key is needed:
the project and databases can be created in the Neon console (the
production bringup did exactly this -- the tier's `neon-admin` key is
deliberately project-scoped and cannot create projects), and since all
three databases share one role/password/endpoint, one copied connection
string yields the other two by swapping the database name. Only the dev
tiers' auto-provisioning path genuinely needs an org-capable key. The
collection loop additionally rides the tier's existing `pool-ssh` Vault
entry (the deploy pushes it as its own Modal Secret, which the
`collection_poll` function attaches) -- every real tier already has it;
nothing analytics-specific to provision there.

## 1. Neon project

In the tier's Neon org, create project `analytics-<env>` (same region as the
tier's other projects, **Postgres major pinned by
`neon_db.ANALYTICS_PG_VERSION`** -- the deploy flow refuses to run analytics
migrations against any other major) with three databases:

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

-- workspace_records carries two columns this role must never read:
-- provider_kind embeds the account email (per-account provider instances
-- are named imbue_cloud_<email-slug>), and encrypted_secrets is the
-- workspace's E2EE secret blob. The analytics identity model keeps emails
-- out of the lakes entirely, so re-grant that table column-by-column.
REVOKE SELECT ON workspace_records FROM analytics_reader;
GRANT SELECT (user_id, host_id, agent_id, display_name, color,
              hosting_device_id, device_label, state, restored_from_host_id,
              revision, created_at, updated_at, destroyed_at, record_format)
    ON workspace_records TO analytics_reader;
```

Use the direct (non-pooler) URI for this role in the Vault entry.

The aggregation SQL must therefore never reference
`rsc.workspace_records.provider_kind` or `.encrypted_secrets` (today it reads
only `user_id`/`created_at`); if a provider-mix dimension is ever wanted,
derive a normalized kind (`imbue_cloud` / `lima` / `docker`) connector-side
first.

A column-scoped grant also blocks reads of the table's `ctid` system column
(Postgres requires table-level SELECT for system columns), which DuckDB's
postgres scanner uses to parallelize scans. The analytics session therefore
sets `pg_use_ctid_scan = false` after every Postgres attach
(`imbue/analytics/lake.py`); without it, even granted-column reads of
`workspace_records` fail with `permission denied`.

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

## 7. One-time backfills (production; run once after the first deploy)

Both close gaps found in the 2026-08-25 production data review; neither is
needed on dev envs or repeatable tiers.

- **Signup dimension.** The connector-side signup sources undercount:
  `account_entitlements` rows are created lazily (100 of 142 real accounts as
  of 2026-08-25) and `account_attribution` only exists for signups since
  2026-08-17 (complete from there on -- it writes at creation on every
  path). Load a static `metrics.gold.accounts_signup(account_id, joined_at)`
  table by paginating the SuperTokens core (`GET <core>/users?limit=100`,
  api-key auth, follow `nextPaginationToken`; each user's `id` +
  `timeJoined`) and inserting the rows through the analyst attach snippet in
  [../reports/README.md](../reports/README.md). Aggregation and reports
  should treat `coalesce(accounts_signup.joined_at, attribution.created_at)`
  as the signup timestamp.

- **Pre-structured-logging raw log archive.** Structured `http_request`
  lines only began with the 0.4.2 deploy (2026-08-25 ~17:15 UTC); the Modal
  access lines before that carry no user attribution and expire with
  OpenObserve's 90-day log retention (the oldest, from the 2026-08-18
  bring-up, around 2026-11-16). If the bringup happens before then, copy the
  OpenObserve bucket's `files/default/logs/**` objects for 2026-08-18
  through 2026-08-25 into the metrics bucket under `archive/pre-0-4-2-logs/`
  (a plain object copy with the logs read token as source) so the
  parse-or-discard decision is not forced by the retention clock. Expected
  analytic value is near zero (no user ids) -- archiving is deliberately the
  cheapest way to defer the decision, not a commitment to parse them.
