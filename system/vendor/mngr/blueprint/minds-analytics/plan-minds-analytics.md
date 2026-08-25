# Plan: minds analytics

## Overview

- Build the minds analytics system on two deliberately different data classes:
  - Coarse, account-keyed usage derived from data we already hold server-side (connector product DB, aggregated logs) — applies to every account.
  - Fine-grained in-workspace data collected only from imbue-hosted workspaces of accounts on the explorer plan — the plan membership *is* the consent.
- Storage is a lakehouse, not a second OLTP schema: DuckLake (validated end-to-end by spike) with Neon Postgres catalogs and R2 parquet data, per env.
  - Two lakes per env with per-bucket access separation: `metrics` (broader analyst access) and `transcripts` (named product owners only).
  - Analysts query with DuckDB from their own machines via read-only Postgres roles + read-only R2 tokens; no hosted dashboard in v1.
- Collection is pull-based and honest by construction:
  - A version-current script is injected over SSH on every run (no analytics code ships in the workspace template), written to an auditable path in the workspace before executing.
  - All redaction (structural strip, secret scan, Presidio) runs inside the container; raw content never crosses the boundary.
  - Every run is recorded both server-side (audit table) and inside the workspace (`collections.jsonl`), and the last-run script is left in place.
  - Users can revoke access unilaterally (authorized_keys); collection just fails and is recorded.
- New monorepo project `apps/analytics` deploying Modal app `analytics-<env>`; deployment is opt-in per env (deploy.toml tier default + sticky `--with-analytics` for dev envs).
- Server-side log feeds ride the just-landed OpenObserve stack by reading its R2 parquet directly (read-only token) — no query API, no tunnel, no second pipeline.
- LiteLLM integration, supervisord app logs, and hosted dashboards are explicitly deferred.

## Expected behavior

### For every account (coarse layer)

- The connector (and litellm proxy, via the shared middleware) emits one single-line JSON access-log record per request instead of the key=value line; authenticated requests additionally carry the full SuperTokens `user` id (never tokens, query strings, or bodies).
- The connector's share-authorize path emits a structured JSON log line per authorized visit: visitor user id, host id, owner id, timestamp.
- An hourly aggregation cron reads the connector product DB (read-only role) and OpenObserve's R2 parquet, and rewrites gold tables in the metrics lake:
  - `activity(account_id, day, signal_type)` — every candidate signal lands as its own row (app-open from sync polls, workspace create/start/stop, share enable, share visit, downloads, signups, and explorer in-workspace signals where present); "active" is defined at query time.
  - `pipeline_health` — last successful run per cron, per-feed staleness, collection failure rates, and cron runtime vs. a warning threshold (so we notice the loop approaching its time budget before it breaks).
  - Funnel and sharing facts from `account_attribution` / `download_events` / share logs.
- Log-derived rows are aggregated into the lake well inside OpenObserve's 90-day retention; the lake is the only long-lived record.
- No new client-side or in-product tracking of any kind is added.

### For explorer accounts (fine-grained layer)

- Every 15 minutes (per-tier configurable) the poll cron enumerates online, non-transitioning imbue-hosted workspaces belonging to explorer-plan accounts; each workspace is collected at most once per hour.
- Collection SSHes to the container sshd with the pool key, injects the current collection script into `data/.imbue/analytics/`, and runs it there; VM-level latchkey signals use the VM-root hop where a VM-side gateway exists. Each hop is independently revocable by the user; a refused hop is recorded and skipped.
- The injected script, in-container:
  - Tails append-only feeds from runner-supplied cursors: common transcripts, `client_activity`, service/server registration events; reads git `--numstat` from the last collected SHA; snapshots workspace state (sharing on/off, installed apps, agent count+types, template version).
  - Redacts transcripts before anything leaves: tool inputs and outputs dropped entirely; message text passes betterleaks + kingfisher + Presidio (deps resolved in-container via `uv` on first run, cached thereafter); roles, tool names, counts, timings, and usage metadata survive.
  - Emits one multiplexed JSONL stream (each line: `source` + payload; final `run_summary` line with counts, new cursors, script version).
  - Leaves the script and a README in place, and appends the run record to `data/.imbue/analytics/collections.jsonl`.
- The runner treats script output as untrusted: per-line and per-run size caps, schema validation of the envelope, malformed lines dropped and counted; validated rows are written directly into the lakes (transcript rows to the transcript lake, everything else to metrics) in one DuckLake transaction; cursors advance after commit; duplicates from cursor-write failures are deduped by event id downstream.
- First contact with an old workspace drains history incrementally under a ~256 MB per-run input budget.
- A downstream in-infra job derives transcript metrics (turns, timings, tool mix) from the transcript lake into the metrics lake.
- Leaving the explorer plan stops collection at the next poll; nothing is deleted. Account deletion removes transcript-lake content for that account and writes a deletion-event fact; metrics-lake raw and gold rows survive keyed by the now-orphaned opaque user id.
- The collection function never logs payload content (its logs flow to OpenObserve); unexpected exceptions go to Bugsink once the Modal-app wiring lands.

### For analysts and operators

- Analysts attach both lakes read-only from their machines (documented snippet in `reports/README`); enforcement is at the Postgres role and R2 token layer (validated by spike).
- `reports/` ships three worked examples: `activity`, `pipeline_health`, `funnel`.
- 30-day DuckLake snapshot expiry everywhere (the undelete window and the physical-deletion bound); no time-based row expiry in v1.
- Operators enable analytics per env: `[analytics]` in deploy.toml (on for staging/production, off for dev/ci) with sticky `minds env deploy --with-analytics` for dynamic dev envs.

## Implementation plan

### New project: `apps/analytics`

- Project scaffolding: `pyproject.toml`, `image_requirements.txt`, `test_ratchets.py`, `conftest.py`, `changelog/`, `LICENSE`, `README.md`.
- `imbue/analytics/app.py` — Modal entrypoint (image, secrets, functions only):
  - `collection_poll` (cron, 15 min): consent diff, enumeration, bounded-parallel collection (~4 workers, per-workspace timeout, duration metric with warning threshold).
  - `aggregation` (cron, hourly): product-DB + log-parquet aggregation into gold tables.
  - `lake_maintenance` (cron, daily): flush inlined data, merge adjacent files, expire snapshots (30 days), cleanup old files, apply deletion events.
- `imbue/analytics/primitives.py`, `data_types.py`, `errors.py` — ids, enums (feed/source names, run outcomes), frozen models (run config, feed records envelope, run summary), error hierarchy.
- `imbue/analytics/lake.py` — DuckDB/DuckLake attach helpers: catalog URLs + R2 secrets from env, metrics/transcripts attach, insert batches, maintenance calls.
- `imbue/analytics/ops_db.py` — psycopg2 access to the `ops` DB: `collection_cursors`, `collection_runs` (audit), `consent_ledger`, `deletion_events`.
- `imbue/analytics/consent.py` — diff explorer-plan membership (connector DB read-only) against the consent ledger; start/stop collection accordingly.
- `imbue/analytics/enumeration.py` — online explorer workspaces: `pool_hosts` (leased, running, not mid-transition) joined to `account_entitlements`.
- `imbue/analytics/collection.py` — the runner: paramiko SSH (container hop + VM hop), script injection, untrusted-output validation (line/run caps, envelope schema), lake writes, cursor advance, audit rows, in-workspace `collections.jsonl` append.
- `imbue/analytics/injected/collect.py` — the injected PEP 723 script (self-contained; pinned deps incl. Presidio): feed readers, cursor-based tailing, redaction pipeline, multiplexed JSONL output, in-workspace README/audit writes.
- `imbue/analytics/aggregation.py` + `imbue/analytics/aggregations/*.sql` — gold-table builds: activity signals from product DB tables and from OpenObserve parquet (access-log user field, share-authorize lines, relay logs), funnel, sharing, pipeline_health.
- `imbue/analytics/deletion.py` — account-deletion path: transcript-lake DELETE by account id, deletion-event fact, invoked by maintenance cron and by `scripts/delete_accounts.py`.
- `imbue/analytics/reports/` — `activity.sql`, `pipeline_health.sql`, `funnel.sql`, `README.md` (attach snippet, credential-minting runbook).
- `migrations/*.sql` — ops DB schema, applied by the deploy flow's schema_migrations runner.

### `libs/modal_app_kit`

- `request_logging.py` — replace key=value formatting with single-line JSON (`type: "http_request"`); read the authenticated user id from ASGI scope state when present; delete the custom quoting helpers; update `request_logging_test.py`.

### `apps/remote_service_connector`

- Auth resolution (`accounts_web.resolve_web_user_identity` path) stashes the full SuperTokens user id into ASGI scope state for the logging middleware.
- `share_broker.py` — emit the share-visit JSON log line on successful authorization.

### `apps/minds` (env/deploy tooling)

- `config/envs/*/deploy.toml` — `[analytics]` block (deploy flag; poll/collect intervals; warm-pool/scaledown as needed); `.minds/template/analytics.sh` secret schema (lake catalog URLs, R2 keys, connector read-only DB URL, OpenObserve bucket read token, Bugsink DSN).
- `envs/provisioning.py` (and friends) — when enabled: create/verify the `analytics-<env>` Neon project (three DBs) and the two R2 buckets + scoped tokens, provision the connector-DB read-only role, run ops migrations, `modal deploy` the analytics app; sticky `--with-analytics` persisted in per-env local state.
- `scripts/delete_accounts.py` — call the analytics deletion path per account.

### `specs/minds-analytics/` (written as part of PR 1)

- `spec.md` — the system spec distilled from this plan.
- `disclosure.md` — plain-language "what we collect from explorer workspaces" (source of truth for future accounts-surface copy).
- `redaction-contract.md` — exact per-record-type field dispositions for transcripts.

### `default-workspace-template` (docs only)

- Short doc explaining `data/.imbue/analytics/` (what appears there, why, and that the script is injected and auditable).

## Implementation phases

Two PRs: phases 1–2 together (with the spec), then phases 3–5.

- **Phase 1 — lakes + coarse aggregation (working system: fleet-wide dashboards).**
  Scaffold `apps/analytics`; provisioning (Neon project, buckets, roles, migrations, deploy flag); lake attach helpers; hourly aggregation from the product DB only (activity from business-table timestamps, funnel from attribution tables); `pipeline_health`; the three reports; spec + disclosure + redaction contract documents.
- **Phase 2 — log-derived signals.**
  JSON access log in `modal_app_kit` + connector user-id stash; share-authorize log line; aggregation extended to read OpenObserve R2 parquet (app-open signals, share visits, relay logins).
- **Phase 3 — collection loop, non-transcript feeds.**
  Consent ledger + enumeration + SSH runner + injected script with `client_activity`, service/server events, git `--numstat`, workspace-state snapshot, VM-side latchkey signals; audit table + in-workspace `collections.jsonl`; cursors; validation and caps.
- **Phase 4 — transcripts.**
  Redaction pipeline in the injected script (structural strip, secret scanners, Presidio via in-container `uv`); transcript lake writes; downstream transcript-metrics derivation job.
- **Phase 5 — deletion + deployment tests.**
  Deletion path wired into maintenance cron and `delete_accounts.py`; dev-tier deployment test of the full loop; cron-duration operational metrics finalized.

## Testing strategy

- **Unit tests (no network):**
  - Redaction: per-record-type field disposition (tool inputs/outputs dropped, envelope survives), secret-scanner gating, Presidio invocation seams; snapshot tests on redacted output shapes.
  - Output protocol: multiplexed JSONL parsing, `run_summary` handling, oversize/malformed line rejection and counting, per-run cap enforcement.
  - Cursor logic: advance/resume, backfill budget, duplicate-tolerant replay.
  - Consent diff, enumeration SQL (against mock stores per connector test conventions).
  - Aggregation SQL against fixture data in a *local* DuckLake (DuckDB-file catalog + local data path — same extension, no Neon/R2).
  - `modal_app_kit` middleware: JSON line shape, user-field presence/absence, control-character safety via JSON encoding.
- **Integration tests:** run the injected script end-to-end inside a local docker workspace (`mngr create` local, docker-marked), asserting on collected JSONL, in-workspace audit artifacts, and idempotent re-runs.
- **Acceptance/deployment tests (dev tier, phase 5):** full poll→collect→query loop against a real dev-env workspace on an explorer test account; deletion path.
- **Edge cases to cover:** workspace offline mid-run; user-mangled script/output; revoked authorized_keys; cursor regression after workspace restore; empty feeds; first-run backfill exceeding the budget; concurrent cron overlap (advisory lock or run-marker).

## Open questions

- Extrapolation methodology: how explorer in-workspace signals calibrate fleet-wide DAU from app-open signals (deferred to analyst iteration in `reports/`).
- Per-table retention windows (v1 has none; decide once real usage is visible).
- Connectors-feed details as the latchkey VM-side layout settles (recently changed; probe-based collection may need updating).
- Dashboard hosting (unhosted Evidence/agents for now; revisit when a non-technical audience appears).
- LiteLLM spend integration (deferred until cloud-minted keys are actually used).
- Accounts-surface disclosure copy (explicitly out of scope for these PRs).
- Per-feed collection backoff tuning once real fleet timing data exists.
