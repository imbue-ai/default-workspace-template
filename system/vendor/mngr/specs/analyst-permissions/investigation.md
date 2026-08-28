# Granting analysts access to production analytics (investigation)

Findings from investigating what a teammate (who is not the operator) needs in
order to build Evidence.dev dashboards against the production analytics stack.
The recommended tooling has since been built: `minds-admin analytics analyst
add|remove|list` (see `apps/analytics/reports/README.md` for usage). This
document records the investigation that led there.

## The access model that already exists

There is no dashboard service and no account system to add people to. Per
`specs/minds-analytics/spec.md` ("Analyst access"), analysts attach the lakes
read-only with DuckDB from their own machines; Evidence is a local
presentation layer over a local DuckDB file. Enforcement lives entirely at the
Postgres-role and R2-token layer, and credentials are deliberately minted
per-person by hand so reads are auditable.

The minting runbook already exists: `apps/analytics/reports/README.md`
("Minting analyst credentials").

## What each teammate needs (metrics-lake dashboards)

An operator with Vault access mints exactly two credentials per person:

1. A personal read-only Postgres role on the `metrics` database of the
   production `analytics-production` Neon project (four statements in the
   reports README: CREATE ROLE, GRANT CONNECT, GRANT USAGE, GRANT SELECT +
   default privileges). Deliver the direct (non `-pooler`) DSN -- DuckLake
   needs session-scoped behavior that PgBouncer's transaction pooling breaks.

2. A personal read-only R2 API token scoped to the
   `analytics-metrics-production` bucket (permission group "Workers R2
   Storage Bucket Item Read"). The S3 access key id is the token id; the
   secret access key is the SHA-256 hex of the token value. They also need
   the Cloudflare account id (not sensitive).

With those plus the monorepo checkout (attach snippet in
`apps/analytics/reports/README.md`, Evidence project pattern in
`apps/analytics/dashboards/`, node >= 20), they can build dashboards
independently. They need no Vault, Neon console, Cloudflare, Modal, or
connector-admin access.

## Caveats

- The Vault `analytics` entry's values are the app's credentials: owner-level
  catalog DSNs and read-write bucket tokens. Never hand those out.
- The transcripts lake (`transcripts` catalog + `analytics-transcripts-production`
  bucket) is restricted to a named list of product owners; mint the same way,
  sparingly, only when genuinely needed. The derived `transcript_daily` /
  `transcript_tools_daily` gold tables are already in the metrics lake.
- The existing box-metrics Evidence prototype uses different sources: a
  read-only token on the tier's OpenObserve bucket plus a read-only role on
  the production connector (host_pool) database. Any connector-DB reader role
  must replicate the column-scoped `workspace_records` grants from
  `apps/analytics/docs/bringup.md` (`provider_kind` embeds the account email;
  `encrypted_secrets` is the E2EE blob). Additionally,
  `extract_box_metrics.py` reads `~/.minds-<env>/secrets.toml`, which only
  exists for auto-provisioned dev envs, so a production run needs a
  hand-written secrets file or a small script change.
- Zero-credential option for page authoring only: run the extract yourself
  and hand over the resulting `.duckdb` file.

## Candidate follow-up work

- A `minds-admin` command to mint (and revoke) analyst credentials -- BUILT:
  `minds-admin analytics analyst add|remove|list`, grants both lakes by
  default with `--no-transcripts` as the opt-out (the same set of people
  currently needs both).
- Production-tier credential resolution for the dashboards extract script
  (Vault-based, like `minds_admin`'s `_tier_secrets.py`). Still open.
