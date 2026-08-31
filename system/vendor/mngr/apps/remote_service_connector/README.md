# remote_service_connector

A lightweight service deployed as a Modal Function that connects minds clients to the remote services they need: SuperTokens authentication, pool-host leasing, LiteLLM keys, R2 buckets, self-hosted workspace sharing, and per-account plans/quotas. All endpoints are authenticated, and every resource grant is checked against the account's entitlements (see "Plans and entitlements" below).

## What it does

Allows authenticated users to:
- Lease pre-provisioned pool hosts, mint LiteLLM keys, and create R2 buckets
- Share workspaces via the self-hosted relay design (`/shares/*`): share records + relay tokens, frps plugin authorization, ACME DNS-01 certificate issuance, and the accounts broker for share login handoff
- See their plan, quotas, and live usage (and switch plans)
- Sign in / sign up via SuperTokens (proxying the SuperTokens core so clients never need its API key)

## Code layout

The service lives in `imbue/remote_service_connector/`:

- `app.py` -- the Modal deployment entrypoint, and nothing else: image, `modal.App`, secrets, function definitions (web app + crons). Deployed by file path; the shipped modules may never import it.
- `web.py` -- FastAPI assembly (mounts every feature router) plus the unauthenticated system endpoints (`/health/liveness`, `/generation`, `/version`).
- Feature modules, each an `APIRouter`: `shares.py` (share records, relay tokens, frps plugin auth), `share_certs.py` (ACME DNS-01 issuance), `share_broker.py` (the accounts broker), `hosts.py`, `llm_keys.py`, `accounts.py`, `sync.py`, `retention.py`, `lease_records.py`, `auth_proxy.py`, and the `r2/` subpackage (`naming`, `stores`, `buckets`, `grants`, `sweep`).
- Foundation modules: `auth.py`, `entitlements.py`, `litellm_client.py`, `cloudflare.py` (raw R2 API client + the shared `CloudflareCtx`), `http_api.py`, `db.py`, `errors.py`, `attribution.py` (the `imbue_attribution` marketing-cookie parser plus the fail-open signup-attribution and download-event writers; see "Download redirect and marketing attribution"), `deploy_constants.py` (the image's pip set).

The container receives only these modules plus `imbue.modal_app_kit` -- nothing else from the monorepo exists at runtime, so shipped modules must not import anything else from it. The rules (and why they exist) are documented in [libs/modal_app_kit/README.md](../../libs/modal_app_kit/README.md) and enforced by `test_project_ratchets.py`.

## Deployment

Deployment is split into two pieces so you can rotate secrets without redeploying code and vice versa.

### 1. Environment-scoped Modal secrets

The committed `.minds/template/*.sh` files declare the expected keys for each service -- they are the schema for the HCP Vault entries at `secrets/minds/<tier>/<service>`. To populate a fresh tier's Vault entry, copy the template into a tmp file, fill in the values, push it to Vault, and shred the local file:

```bash
cp .minds/template/cloudflare.sh /tmp/cloudflare-production.sh
$EDITOR /tmp/cloudflare-production.sh
uv run scripts/push_vault_from_file.py production cloudflare /tmp/cloudflare-production.sh
shred -u /tmp/cloudflare-production.sh
```

Each template file is shell-style:

```sh
# .minds/template/cloudflare.sh
export CLOUDFLARE_API_TOKEN=
export CLOUDFLARE_ACCOUNT_ID=
# ...
```

Push everything to Modal and deploy in one shot:

```bash
eval "$(uv run minds-admin env activate production)"
uv run minds-admin env deploy --yes-i-mean-production
```

`minds-admin env deploy` reads `apps/minds/imbue/minds/config/envs/production/deploy.toml`
for the list of services to push from Vault, creates/updates Modal
secrets named `<service>-<env>` (e.g. `cloudflare-production` and
`supertokens-production`), then runs `modal deploy` for the
connector and the LiteLLM proxy. The push aborts with a diagnostic if
any Vault entry is missing a key declared by the template (empty
values are fine -- the deploy skips them when pushing to Modal).

The connector reports errors to the tier's self-hosted Bugsink instance
(an operator-lifecycle VPS, provisioned via `apps/observability`) through
`imbue.modal_app_kit.sentry` -- a no-op until the tier's `sentry`
Vault entry carries `RSC_SENTRY_DSN`, and disabled entirely by
`MINDS_SENTRY_DISABLED=1` (see `specs/minds-bugsink-error-tracking.md`).
The reporting policy:

- Every connector-defined exception inherits `errors.ConnectorError`; the
  EXPECTED ones are exactly those `http_api.raise_as_http` maps to status
  codes. Anything else escaping a route reaches the app-level 500 handler
  (`http_api.handle_unexpected_exception`), which reports it at error
  (top) priority and answers `{"detail": {"code": "internal_error",
  "message": ..., "event_id": ...}}` -- the exception text itself is
  included only on dev/ci tiers, never on production/staging.
- `logger.error` and `logger.warning` both become Bugsink events; warning
  is the lower-priority channel for exceptions the code caught and
  continued past for robustness. Expected, routine anomalies (transient
  upstream errors, client-input junk) are instead counted as `metric`
  JSON log lines (`imbue.modal_app_kit.metrics`) that flow into the
  tier's OpenObserve via Modal's OTEL integration, so their rates are
  chartable without polluting the error tracker.
- Cron/spawned Modal functions report through `capture_and_reraise`.
- Every log line the container emits is one JSON object with an explicit
  `level` (`imbue.modal_app_kit.log_format`, installed by `configure_logging`
  at the top of each Modal function): Modal's OTEL exporter stamps every
  line `INFO`, so severity queries over `modal_logs` use
  `spath(body, 'level')`. Our `imbue.*` loggers emit at INFO
  (raise a dev env with `MINDS_LOG_LEVEL=DEBUG` at deploy time); third-party
  libraries stay at WARNING.
- `GET /health/reporting-probe` (dev/ci tiers only; disabled on
  production/staging) deliberately exercises every channel in one request
  -- a metric line, a warning event, and an unmapped exception through the
  500 handler -- so the deployment-test suite can prove the pipeline end
  to end (`apps/minds/deployment_tests/test_error_reporting.py`).

**cloudflare.sh** holds the Cloudflare API credentials (R2 buckets + ACME DNS-01 TXT records; the tunnel/Access stack is gone):

- `CLOUDFLARE_API_TOKEN` (required): account-owned API token; see "Cloudflare token requirements for R2" below.
- `CLOUDFLARE_ACCOUNT_ID` (required): Cloudflare account ID.
- `CLOUDFLARE_ZONE_ID` (required): Cloudflare zone ID (used for the ACME DNS-01 challenge TXT records).
- `CLOUDFLARE_DOMAIN` (required): Base domain for the tier's DNS records (read by the tier setup scripts, not by the connector).

**supertokens.sh** holds the SuperTokens + OAuth credentials:

- `SUPERTOKENS_CONNECTION_URI` (required): URL of the SuperTokens core.
- `SUPERTOKENS_API_KEY` (required for most deployments): SuperTokens core API key.
- `AUTH_WEBSITE_DOMAIN` (required whenever `SUPERTOKENS_CONNECTION_URI` is set): Public base URL embedded in password-reset and email-verification links. Must match the URL Modal assigns to the deployed function. There is no derived fallback: if unset, `init_supertokens()` raises `MissingAuthWebsiteDomainError` at container startup, so populate it in the per-tier `supertokens-<env>-<deploy-id>` Modal secret (the deploy script pushes it from the tier's Vault entry).
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (optional): override Google OAuth client credentials. Leave blank to inherit from the SuperTokens core's dashboard.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` (optional): override GitHub OAuth client credentials. Leave blank to inherit from the SuperTokens core's dashboard.
- `MINDS_ADMIN_KEY` (optional): fixed API key authenticating the operator admin endpoints -- the paid-list CRUD (`/paid/*`), the account admin API (`/admin/accounts/*`), and the on-demand sweeps (`/admin/sweep/*`). Distinct from every other auth path -- the connector accepts it ONLY on those routes and rejects SuperTokens tokens there, and rejects this key on every other route. Leave empty to disable the admin API. The `minds-admin ...` CLI reads the same value from `$MINDS_ADMIN_KEY`. The deprecated `MINDS_PAID_ADMIN_KEY` spelling is still accepted (with a warning) while Vault entries and operator environments migrate.
- `MINDS_PAID_LIST_CACHE_TTL_SECONDS` (optional): how long (seconds) the connector caches a per-email paid-status lookup before re-querying the tables. Unset uses the built-in default (60s); `0` disables caching. Each container caches independently, so a paid-list change propagates within this window.

### Plans and entitlements (quotas)

Resource access is governed by per-account quotas ("entitlements"), not by a paid/unpaid gate:

- The `plans` table holds the plan definitions ("free", "explorer", and "ally" today). It is **git-owned**: `minds-admin env deploy` writes (overwriting) the `[plans]` blocks from the tier's `deploy.toml` after migrations, so deploy.toml is the source of truth for plan defaults.
- The `account_entitlements` table holds one row per account, created lazily on the account's first quota-relevant request. The row's values are copied wholesale from the plan at assignment and are the adjustable source of truth thereafter -- changing a plan's defaults never retroactively changes existing rows.
- The hosted signup form offers "free" and "explorer" (explorer recommended and preselected); the chosen plan's row is created at account creation, on both the password and Google paths (the choice rides the OAuth state JWT). The write fails open: a lost choice degrades to the lazy backfill below.
- Lazy-creation backfill rule: accounts whose SuperTokens `time_joined` predates the feature-ship cutoff get "ally" when their email is paid-listed; every other account without a recorded signup choice backfills as "free". Explorer-plan membership is the in-workspace analytics-collection consent (see `specs/minds-analytics/spec.md`), so it is only ever assigned by an explicit user choice -- the signup selector or a later plan switch -- never by a fallback.
- Account creation also requires agreeing to the Terms of Service and Code of Conduct. The signup form gates both creation paths client-side; for Google, the agreement rides the OAuth state, and a new-account exchange arriving without it (the sign-in tab's button) is rolled back and bounced to the login page's `terms_required` banner. The linked documents are static HTML pages shipped in the accounts bundle and served at `/terms-of-service`, `/code-of-conduct`, and `/privacy-policy` (the plan selector's "Learn more" target).
- Quota rejections are HTTP 403 with structured detail: `{"code": "quota_exceeded", "entitlement": "<name>", "limit": N, "current": N, "message": "..."}`.
- Quotas are checked when a resource is *granted* (lease, bucket, sync record, key, share). Lowering a quota below current usage never revokes existing resources; the two continuous exceptions are the monthly LLM budget (enforced per-request by LiteLLM user budgets) and R2 storage (enforced by the hourly sweep, see "R2 storage-quota sweep" below).

The quota entitlements: `max_remote_workspaces`, `max_buckets`, `max_total_bucket_bytes`, `monthly_llm_spend_usd`, `max_active_synced_workspaces`.

### Paid lists (ally-plan eligibility)

The paid lists remain, but only as the eligibility input for selecting the "ally" plan:

- `paid_emails` -- exact, full-email matches (e.g. `bob@gmail.com`).
- `paid_domains` -- exact domain matches on the part after `@` (e.g. `imbue.com` matches `alice@imbue.com` but NOT `alice@eng.imbue.com`).

An email is "paid-listed" when it (or its exact domain) has an active (`is_paid = true`) row in either table. Both tables are managed via the `/paid/*` CRUD endpoints (admin-key authenticated) or the `minds-admin paid` CLI. Rows are never hard-deleted -- "remove" sets `is_paid = false`. Removing an email from the list does NOT automatically demote an existing ally; that is an operator action via the account admin API. The schema is created by `migrations/005_paid_lists.sql`.

On deploy, `minds-admin env deploy` seeds each tier's configured default entries (the `[paid]` block in that tier's `deploy.toml`) into these tables right after migrations. Every tier currently defaults `domains = ["imbue.com"]`. Seeding is **seed-if-absent** (`INSERT ... ON CONFLICT DO NOTHING`), so it sets the initial default but never re-activates an entry an operator soft-removed.

### Cloudflare token requirements for R2

The R2 bucket routes require `CLOUDFLARE_API_TOKEN` to be an **account-owned** token (`cfat_`) -- not a user-owned token (`cfut_`) -- because the connector mints account-owned per-bucket R2 tokens on the user's behalf. The token needs these permissions:

- `DNS: Edit` (on the tier zone; ACME DNS-01 challenge TXT records for shared-workspace certificates)
- `Workers R2 Storage: Edit` (R2 buckets)
- `Account API Tokens: Edit` (mint/revoke/roll per-bucket R2 keys)
- `Account Analytics: Read` (the storage-quota sweep's GraphQL usage query)

**R2 must also be enabled on the Cloudflare account** (a one-time dashboard action; until then the API returns `code 10042 "Please enable R2 through the Cloudflare Dashboard"`). Existing tiers shipped with a user-owned DNS token and must be migrated (create the account-owned token with the permissions above, replace `CLOUDFLARE_API_TOKEN` in Vault, then redeploy) before the bucket routes work.

### 2. Deploy the Modal app

The previous step (`minds-admin env deploy --yes-i-mean-production`) already
runs `modal deploy` for the connector as part of the unified deploy
flow. If you want to re-deploy just the connector (e.g. after editing
`app.py` without changing any Vault secrets), invoke `modal deploy`
directly:

```bash
MNGR_DEPLOY_ENV=production uv run modal deploy --name remote-service-connector-production \
    --env main apps/remote_service_connector/imbue/remote_service_connector/app.py
```

`MNGR_DEPLOY_ENV` is read at module load by `app.py` to pin the
secret names (`cloudflare-production`, `supertokens-production`).
Running `modal deploy` directly without the wrapper defaults to
`production`.

## Authentication

All non-`/auth/*` endpoints require a Bearer token, with the exceptions noted below:

- **User (SuperTokens JWT)**: `Authorization: Bearer <access_token>` — the signed-in user's SuperTokens session. A signed-in user has full authority over their own resources; their user-id prefix (the first 16 hex chars of their SuperTokens user ID) namespaces their leases and buckets.
- **Email verification is non-blocking**: an unverified account authenticates like any other. A verified email is required only for the actions where the email is an authorization identity (the `require_verified_email` guard: satisfying a share grant as a visitor via the accounts broker, and ally-plan eligibility -- the explicit plan switch and the lazy pre-cutoff backfill) plus, as a spam/abuse mitigation, creating a remote workspace (`POST /hosts/lease` and `POST /hosts/claim`). Gated endpoints return a structured 403 `{"code": "email_not_verified", ...}` so clients can prompt verification contextually; the workspace-creation gate additionally sends the verification email itself (cooldown-limited) and reports that in the detail's `sent` field. Nothing ever auto-marks an email verified (the old paid-list auto-verification is gone); the admin-key test-signup endpoint is the sole, operator-trust exception.
- The share-certificate endpoint (`POST /shares/cert`) is instead authenticated by the share's relay token (see `share_certs.py`), and the frps plugin callback by its shared secret (see `shares.py`).
- **Browser sessions (the hosted accounts surface)**: the `/login` / `/signup` / `/manage` pages and their `/accounts/api/*` JSON API use SuperTokens' native cookie-based sessions (the SDK middleware serves refresh under `/accounts/auth/`). Sessions roll via refresh but are capped at ~30 days from creation: a creation-time stamp in the access token payload (which survives refreshes) is checked on every resolution, and a session past the cap -- or without a readable stamp -- is revoked on sight. The cap lives in the connector because the SDK cannot configure the SuperTokens core's refresh-token validity. The device handoff (`GET /accounts/authorize` + `POST /auth/device/token`) mints independent bearer-token sessions for the desktop app / CLI; those are not subject to the browser-session cap (clients hold a refresh token and rotate it).
- The share-authorization route (`GET /share/authorize`) resolves the same browser session; `GET /share/jwks.json` is public (it serves only the broker's verification keys).
- The download redirect (`GET /download`) is public -- it serves the marketing site's download buttons (see "Download redirect and marketing attribution" below).

The `/auth/*` endpoints are themselves the authentication flow, so they do not require a token.

### Signup IP hardening

Account creation on the hosted accounts surface (the Turnstile-gated password form and the Google OAuth callback's new-account branch) is additionally gated on the client IP (`signup_hardening.py`); returning sign-ins are untouched. The trusted IP is the ASGI socket peer -- Modal's ingress delivers the real client there and strips `X-Forwarded-For`, while other forwarding-style headers pass through unsanitized and are never consulted (see `modal_app_kit`'s `client_ip_from_asgi_scope`).

- **Velocity limits**: per-IP (hourly) and per-subnet (/24 v4, /48 v6, daily) caps counted from the Neon `signup_attempts` table. Refusals answer status `RATE_LIMITED`.
- **Reputation bands** from the IPinfo Max lookup API (`IPINFO_TOKEN` in the supertokens secret; lookups are cached per IP in `ip_reputation_cache` and budget-capped per day), unioned with an hourly-refreshed Tor-exit-list check that needs no token: Tor/hosting IPs are blocked outright (`SIGNUP_BLOCKED`; a Google-created account is rolled back), and vpn/proxy/relay IPs (residential proxies included, on the IPinfo Max plan) are stepped up to OAuth-only (`OAUTH_ONLY` -- the password form is refused, Continue with Google still works).
- **Fail-open everywhere** (deliberately the opposite of Turnstile, which fails closed): a Neon, IPinfo, or Tor-list outage degrades signup to "Turnstile + whatever signal remains" with a warning log.
- **Every gated attempt is recorded** (allowed ones included) with its IP, subnet, verdict, and outcome in `signup_attempts`, so a flood is visible in real time rather than reconstructed from Modal logs afterwards.
- Enforcement applies on the tiers whose signup is restricted to the hosted surface (production/staging, the same line as the JSON-signup refusal); dev/CI tiers record verdicts but never refuse.

### Quota enforcement

Every resource-granting endpoint checks the caller's entitlements (see "Plans and entitlements" above) on top of user auth:

- `POST /hosts/lease` -- `max_remote_workspaces` (strict: a per-user advisory lock serializes concurrent leases; stopped workspaces still hold their lease and count). Also requires a verified email, like `POST /hosts/claim` (see "Email verification is non-blocking" above).
- `POST /buckets` -- `max_buckets`, plus `max_total_bucket_bytes` against live REST-measured usage (an account already over its storage quota cannot create new buckets; an unreadable usage number fails open). New keys minted while the owner is enforced-over-quota (bucket creation and roll-key's fresh mint) come out read-only with the downgrade recorded, so a fresh mint can never bypass the sweep.
- `POST /keys/create` -- refused outright when `monthly_llm_spend_usd` is 0 (e.g. the free and explorer plans); otherwise the account's LiteLLM user-level budget is upserted before minting, so LiteLLM caps aggregate spend across all the account's keys.
- `PUT /sync/records/by-workspace/{workspace_id}` (and the deprecated host-keyed shim `PUT /sync/records/{host_id}`) -- `max_active_synced_workspaces` when the push would create a new ACTIVE record.

### Paid-list admin API (`/paid/*`)

The paid lists are managed by a separate set of endpoints authenticated by the fixed `MINDS_ADMIN_KEY` (passed as `Authorization: Bearer <key>`). This key is rejected on all other routes, and SuperTokens tokens are rejected here. All operations are idempotent; `list` returns every row with its `is_paid` status by default (`?paid_only=true` filters to active rows):

- `GET /paid/domains` / `GET /paid/emails` -- list rows.
- `POST /paid/domains/add` / `POST /paid/emails/add` -- body `{"value": "..."}`; add or reactivate.
- `POST /paid/domains/remove` / `POST /paid/emails/remove` -- body `{"value": "..."}`; soft-delete (`is_paid = false`).

## API

### Shares (signed-in user; self-hosted workspace sharing)

A shared workspace lives at `<service>.<share-label>.<user-hash>.<region>.<content-domain>` behind a self-hosted frps relay (the share label is minted and persisted on the share row; the user hash is one-way so certificate-transparency logs never reveal the account id). Shares created by clients that predate workspace-keyed sharing keep their legacy `<service>.<host-id>.<user-label>.<region>.<content-domain>` domains -- a re-share never changes an existing share's domain. The connector owns the share records, relay tokens, and certificate issuance:

- `POST /shares` -- Enable sharing for one workspace. Body: `{"host_id": "host-<32hex>", "workspace_id": "agent-<32hex>"}` (`workspace_id` -- the workspace's durable identity -- keys the share so it follows the workspace across machines; old clients may omit it and get a legacy host-id-led domain), plus an optional `preferred_region` (honored only for hosts the connector has no datacenter record of, e.g. local workspaces; a re-share always keeps the share's existing region). Returns the workspace domain, the relay endpoint the workspace's frpc should dial, and the plaintext relay token (returned exactly once; only its hash is stored). Re-sharing rotates the token.
- `GET /shares` -- List the caller's share records (active and inactive).
- `GET /shares/relays` -- The region -> relay tunnel-control endpoint map plus the default region, so clients can pick a `preferred_region` by measuring their own latency.
- `DELETE /shares/{host_id}` -- Disable sharing (share goes `inactive`, relay token deleted).
- `GET /shares/{host_id}/status` -- One share's domain, tunnel-liveness signal, certificate expiry, and the chrome's entry label.
- `POST /shares/cert` -- Sign the workspace's CSR via ACME DNS-01 (authenticated by the share's relay token; the workspace keeps its private key).
- `POST /frps/auth/{relay_id}` -- The frps server-plugin callback authorizing relay `Login` / `NewProxy` / `Ping` operations, authenticated by the shared plugin secret delivered as an `Authorization: Basic` header (the relay's rendered plugin `addr` carries it as URL userinfo, so it never lands in access-logged paths; `FRPS_AUTH_SECRET` accepts a comma-separated set so rotations can briefly accept old + new). The legacy path-secret shape `POST /frps/auth/{plugin_secret}/{relay_id}` is still accepted while pre-rollout relays remain deployed (its structured access-log lines carry a redacted path). An allowed `NewProxy` also records the workspace's shell-service label as the share's chrome entry origin (the connector never reads anything from inside the workspace). A `Ping` (the workspace's ~10s heartbeat) is rejected when its relay token no longer resolves to an active share -- severing the LIVE tunnel of a suspended or freshly unshared workspace -- and fails open on connector-internal errors so tunnel uptime is coupled only to the connector being reachable. Allowed Ping decisions are cached in-process for `MINDS_FRPS_PING_CACHE_TTL_SECONDS` (default 30s, `0` disables), so the sever takes effect within one heartbeat interval plus at most that TTL; rejects and fail-opens are never cached, and `Login`/`NewProxy` always hit the DB. Successful pings emit no per-request access-log line; their rate and duration flow into OpenObserve as periodic `frps_ping_authorized` / `frps_ping_authorized_duration_ms_total` metric records per relay (rejected and errored pings still log in full).
- `GET /share/authorize`, `GET /share/jwks.json` -- the accounts broker: authorizes a visit to a shared workspace against the hosted accounts surface's browser session and mints the short-lived handoff JWT (`GET /share/login` survives only as a permanent redirect to the merged `/login` page).

### Buckets (signed-in user only)

R2 buckets give an account remote object storage. Each bucket is isolated (one per host the user makes); isolation is per-bucket, not per-prefix. Buckets are named `<user_id_prefix>--<slug>` where `user_id_prefix` is the caller's 16-hex SuperTokens prefix; the server re-checks that prefix in code (not just via the R2 `name_contains` filter) so a crafted name cannot grant cross-user access. Each bucket has exactly **one** key; the hourly sweep revokes any extras (newest wins).

- `POST /buckets` -- Create a bucket and mint its single key. Body: `{"name": "...", "access": "read"|"readwrite"}`. Returns `{bucket, key}` where `key` includes the one-time `secret_access_key`. Errors `409` if the derived bucket already exists, `403` (quota) at the `max_buckets` cap, `400` on an invalid derived name.
- `GET /buckets` -- List the caller's buckets.
- `GET /buckets/{name}` -- Bucket metadata (full R2 name + S3 endpoint). Keys come from the key routes.
- `DELETE /buckets/{name}` -- Destroy a bucket. Returns `409` if the bucket is not empty (empty it first); on success, cascades -- revokes all of the bucket's keys and deletes their rows.
- `POST /buckets/{name}/roll-key` -- Return fresh credentials for the bucket's key by rolling its secret in place: same Access Key ID, new Secret Access Key, token policies untouched (so a storage-quota downgrade survives a roll). Mints a fresh key when the bucket has none.
- `GET /buckets/{name}/keys` -- List the caller's keys for one bucket (no secrets).
- `GET /bucket-keys` -- List all of the caller's keys across every bucket (no secrets).
- `DELETE /bucket-keys/{access_key_id}` -- Revoke a key by its Access Key ID and drop its row (recover with roll-key, which then mints anew).

Each key is an account-owned Cloudflare API token scoped to the one bucket; the S3 Access Key ID is the token id and the Secret Access Key is the SHA-256 of the token value (returned once, never stored). Only key *metadata* (access key id, owner, bucket, scope, alias, created_at, enforcement state) is persisted, in the `r2_keys` table; buckets themselves are listed straight from the R2 API.

### R2 storage-quota sweep

An hourly cron (`r2_quota_sweep`) enforces each account's `max_total_bucket_bytes`:

- Usage comes from one Cloudflare GraphQL analytics query per sweep (`r2StorageAdaptiveGroups`, grouped by `bucketName` only, so it returns exactly one row per bucket: the peak snapshot inside a 3-hour lookback). One query covers every bucket, so the sweep's API cost does not scale with bucket count; a response that fills the row budget (possible truncation) fails the cron run loudly instead of enforcing from partial data. The real-time per-bucket REST usage endpoint serves the display path (`GET /account`).
- The GraphQL peak is only a screening filter: before any key is downgraded, the owner is re-measured with the real-time REST usage endpoint (the same source the recheck endpoint reads), so a user who just cleaned up is never re-downgraded on a stale window peak. Restores need no confirmation -- a peak under the limit proves live usage is under it.
- An over-quota account's readwrite keys have their token policies flipped to read-only **in place** -- the S3 credentials are unchanged, so reads keep working while writes fail -- and are restored automatically once the account is back under quota.
- The sweep skips accounts with an active cleanup grant (see below), settles expired grants, and enforces the single-key-per-bucket invariant on every pass.
- `POST /admin/sweep/r2` (admin-key authenticated, like `/admin/accounts/*`) runs one sweep pass on demand; an optional `?email=` query parameter scopes it to a single account. Used operationally and by the deployment tests.

### Storage-cleanup grants

Cloudflare's R2 token model has no delete-without-write permission, and restic's space reclaim (`forget` + `prune`) needs full write access (prune repacks data). So an over-quota account with read-only keys could never reduce its own usage. Cleanup grants close that loop:

- `POST /account/storage-cleanup-grant` (SuperTokens auth) flips all of the caller's downgraded keys back to readwrite and records a grant with the live usage as its baseline. Idempotent: an active grant is returned as-is, and an account with nothing downgraded gets a `not_needed` no-op.
- `POST /account/storage-recheck` re-measures live usage and applies enforcement immediately (restoring or downgrading), settling any outstanding grant. It also works standalone -- a user who freed space some other way does not wait for the hourly sweep.
- A grant settles as *successful* when usage decreased at all versus its baseline. Only unsuccessful grants count against a rolling budget (5 settled-without-decrease grants per 24 hours; a 403 with `code: cleanup_grant_budget_exhausted` past that), so genuine cleanup is unlimited while write-under-cover-of-cleanup abuse is bounded to roughly one sweep interval of writes per burned grant.
- Grants expire after 60 minutes; the sweep settles expired grants as the fallback when the client never rechecked, and skips enforcement for accounts whose grant is still active (a prune transiently *increases* usage while it repacks).
- Enforcement flips are serialized per account by a DB lease; while another enforcement pass (sweep, grant, recheck, or suspension) holds the account's lease past a bounded wait, these two endpoints answer a retryable 503 with `code: enforcement_busy` (or `enforcement_interrupted` when a pass was taken over mid-run) -- the client simply retries shortly.

### Destroyed-workspace backup retention reaper

Destroyed workspaces' backups (bucket + workspace record) are retained for 30 days, then reaped. An hourly cron (`backup_retention_reap`) is the server-side backstop (minds' client-side reaper does the same work faster where a client runs; every step is idempotent so the two never conflict):

- Workspace-backup buckets are associated with their record primarily through the record's explicit `backup_bucket` column; name derivation is the legacy fallback. New buckets are named by the workspace id (`agent-<hex>`, the workspace's system-services agent id); buckets provisioned before the workspace-keyed naming carry the machine's host id (`host-<hex>`) and are grandfathered. Both short-name shapes are reserved: `POST /buckets` refuses them unless a workspace record backs the name for the caller, and `DELETE /buckets/{name}` refuses such a bucket while its workspace record is still ACTIVE (tombstone-first is enforced server-side).
- Records carry a server-stamped `destroyed_at` (set on the transition to `state = destroyed`, kept across destroyed-state updates, cleared on resurrection). Destroyed records older than the window lose their bucket first, then the row -- a failed or partial bucket delete leaves the row for the next pass.
- Workspace-backup buckets referenced by **no** record at all (orphans) age from a first-seen stamp in the `orphan_backup_buckets` table; the migration's stamp-on-first-sight semantics double as the rollout grace period for pre-existing leftovers.
- Emptying is bounded per pass (record + object budgets) and resumable, so one cron invocation never runs long; a partially-emptied bucket continues on the next pass and the deletion lands on the pass that finishes.
- `GET /policies/destroyed-workspace-backups` (public) serves the retention window to clients.
- `POST /admin/sweep/backup-retention` (admin-key authenticated) runs one reap pass on demand. `?dry_run=1` returns the candidate list (kind, ids, stamps) without deleting anything; `?window_seconds=<n>` overrides the window (admin-only; e.g. `0` lets a deployment test reap a fresh tombstone).

### Workspace records and pool leases (the lease invariant)

A pool lease (`pool_hosts`) and a synced workspace record (`workspace_records`, see the `/sync/*` routes) are two views of one cloud workspace. Records are client-driven in general (their secrets blob is encrypted under a per-account key only clients hold), but the connector keeps the two views consistent at the points only it controls:

- `POST /hosts/lease` and `POST /hosts/claim` insert a metadata-only ACTIVE record stub (display name, `provider_kind = imbue_cloud_<account-slug>`, no secrets) **in the same transaction** as the lease grant, so a lease without a record never exists, even transiently. The owning desktop's reconcile enriches the stub through the normal CAS push (its first push conflicts on the stub's revision and rebases). Side effect: CLI-created workspaces appear in every signed-in client's list.
- `POST /hosts/{id}/release` (and the operator release, the failed-claim rollback, and the sweep below) retires the workspace's ACTIVE record in the same transaction as the row's flip to `removing`: a record a client has written to is tombstoned (`state = destroyed`, `destroyed_at` stamped, revision bumped), while a record still at its lease-time stub (revision 1, no secrets, no backup bucket -- a create or claim that failed after the lease, or a workspace with nothing to recover) is deleted outright so no ghost appears in "recently destroyed". Either way a `mngr destroy` from the CLI can no longer leave an ACTIVE record behind.
- **Tombstone-first**: `DELETE /sync/records/by-workspace/{workspace_id}` (and the host-keyed shim) answer `409 {"code": "lease_active"}` while the caller holds a pool lease for the workspace, in any lifecycle status (`leased`, `stopping`, `stopped`, `starting`, `crashed`, `removing`). Destroying the workspace is what releases the lease; "remove from list" cannot manufacture a lease no client shows. The backup-retention reaper honors the same rule: a tombstone whose lease still exists is never reaped, so the sweep's evidence survives the 30-day window.
- **The lease-vs-record sweep** (`lease_record_sweep`, hourly at :20; `POST /admin/sweep/lease-records` on demand, `?dry_run=1` to list verdicts, `?grace_seconds=<n>` to override the window) joins every lease-holding row against its owner's record. A lease whose record is a tombstone older than the 6-hour grace window is released through the exact release chain above (the user's destroy intent is durable and the release evidently failed); a row still `removing` past the same window (its flip is the same kind of durable destroy intent; a fresh flip is a release still in flight and is left to its caller) is re-driven the same way (its record may already be gone: a never-written stub is deleted in the same transaction as the flip); a lease that is not `removing` and has **no record at all** is impossible through legitimate paths, so it is reported (one warning per pass) and never auto-reaped. Per-kind counts flow out as `lease_record_drift` metric records. Releases are bounded per pass and a failed release is confined to its row (left `removing` for the next pass).

The release chain itself holds no DB connection across its SSH/S3 work (intent flip + record retirement in one short transaction, then teardown, then the row delete on a fresh connection), and a `limactl delete` that finds its instance or disk already absent counts as done, so a release interrupted between the teardown and the row delete converges on retry instead of wedging in `removing`.

### Pool gauges (the fleet-version dashboards' source)

The **pool-gauge sweep** (`pool_gauge_sweep`, every 5 minutes; `POST /admin/sweep/pool-gauges` on demand, admin-key authenticated) reads the pool and emits it as `metric` log records for the OpenObserve dashboards (`apps/observability`): `pool_hosts_count` per (status, template branch, region) -- zero-filled over the known-status x observed-branch x known-region cross-product so a drained series reports 0 instead of going stale -- plus per-region `pool_slots_total` / `pool_slots_used` over ready boxes (occupancy = pool rows with a `bare_metal_server_id`; the bake's on-box check remains the real capacity guard) and a `pool_gauge_sweep_ok` heartbeat. Pure observation (two SQL reads, no external APIs), deliberately separate from the control-loop sweeps. Each `/hosts/lease` / `/hosts/claim` attempt that reaches host selection also emits one `host_lease_request` metric record tagged with its outcome (`leased` / `pool_exhausted` / `no_host_keys` / `injection_failed`) and the requested region and branch (client-supplied values are clamped to a conservative shape so they cannot mint arbitrary metric series).

### Account (signed-in user only)

- `GET /account` -- The caller's plan, entitlement values, live usage, and the available plan names. Lazily creates the entitlements row on first touch.
- `POST /account/plan` -- Switch plans. Body: `{"plan": "..."}`. Resets the account's entitlements wholesale to the plan's defaults; re-selecting the current plan is a no-op (idempotent retries never wipe operator-granted bumps). Switching to "ally" requires a paid-listed email (403 with the reason otherwise).

### Account admin API (`/admin/accounts/*`)

Email-addressed operator management of per-account entitlements, authenticated by the same fixed `MINDS_ADMIN_KEY` as the paid-list CRUD (and exposed as `minds-admin account ...`):

- `GET /admin/accounts/{email}` -- One account's plan, entitlements, live usage, and suspension state (lazily creates the row).
- `POST /admin/accounts/{email}/plan` -- Body `{"plan": "..."}`; always resets to the plan's defaults (the operator's way to wipe manual bumps; skips the ally eligibility check).
- `POST /admin/accounts/{email}/quota` -- Body `{"entitlement": "...", "value": N}`; bump a single entitlement.
- `POST /admin/accounts/{email}/revoke-sessions` -- Revoke every SuperTokens session of the account (standalone; sign-in stays possible). State-modifying routes verify Bearer sessions against the core per request, so a revoked session is refused within one round-trip while read access drains out over the access token's remaining ~1h lifetime.
- `POST /admin/accounts/{email}/suspend` -- Body `{"reason": "...", "block_storage": bool}`. Reversible, data-preserving suspension: sets the flag (every session-creation/refresh path then answers the structured `account_suspended` refusal), revokes all sessions, force-stops leased workspaces, blocks LiteLLM keys, flips R2 tokens read-only (or disables them outright under `block_storage` -- reads included), and suspends shares by state (relay tokens kept). Idempotent and re-runnable with a per-step report; re-running with `block_storage` escalates, re-running without it never de-escalates. The reason is operator-internal; users see a generic message with the support contact.
- `POST /admin/accounts/{email}/unsuspend` -- Clear the flag and restore what suspend changed (unblock keys, restore R2 access per the quota state, reactivate shares -- tunnels resume on their own once the workspace runs, since the workspace still holds its relay token). Workspaces stay stopped until the user starts them; the user signs in fresh.
- `POST /admin/workspaces/{host_db_id}/stop` -- Operator force-stop of one workspace (the owner stop transition without the ownership check; used by suspension, migrations, and future idle shutdown).
- `POST /admin/workspaces/{host_db_id}/release` -- Operator release of one workspace regardless of owner: the owner's exact release chain (stop artifacts deleted, slice VM destroyed, workspace record retired, row dropped), for any lifecycle status including `stopped`. Idempotent (`already_released` for a row that is gone). Exposed as `minds-admin workspaces release`.

There is deliberately **no account-deletion endpoint** here: fully removing a user (its SuperTokens identity plus every connector-DB row keyed to it) is a destructive operator action done out-of-band, not something the connected clients need. Use the local operator tool `scripts/delete_accounts.py` (repo root) for that -- see "Fully deleting accounts" below.

### Fully deleting accounts (`scripts/delete_accounts.py`)

`scripts/delete_accounts.py` is a **local** operator tool (private -- not in the public-mirror allowlist, and it lives at the repo root, not in this app) that fully deletes accounts by talking directly to a tier's live backends, so it needs **no connector deploy**. For each account in a CSV it removes the connector-DB rows keyed to the user (`account_entitlements`, `workspace_records`, `account_key_bundles`, `r2_cleanup_grants`, and `shares` / `relay_tokens` keyed by the account's 32-hex share label), best-effort-deletes the LiteLLM internal user, and deletes the SuperTokens identity itself last (so a partial run is safe to re-run).

It is **dry-run by default** (prints the plan, changes nothing until `--execute`), refuses any account still holding a leased pool host (release those via `mngr pool destroy` first), tolerates target tables absent from a tier's DB, and leaves each account's `host-<hex>` R2 backup buckets to the backup-retention reaper (deleting the workspace records orphans them). Credentials resolve per value from an explicit flag, else an environment variable, else the tier's HCP Vault entries.

```bash
export VAULT_TOKEN=...   # or a prior `vault login`
# Dry run (default): show exactly what would be deleted for every account in the CSV.
uv run python scripts/delete_accounts.py --tier production --accounts-file accounts.csv
# Actually delete:
uv run python scripts/delete_accounts.py --tier production --accounts-file accounts.csv --execute
```

The accounts file is a CSV with a header row containing a `user_id` column (an `email` column, when present, is used only for reporting).

### Download redirect and marketing attribution (unauthenticated)

- `GET /download?platform=...` -- Public redirect the imbue.com marketing site's download buttons point at: records a campaign-tagged `download_events` row and 302s to the platform's installer (`mac-arm64`, alias `mac` -> the arm64 `.dmg` the **stable release channel** serves, read from `stable-mac.yml` and cached briefly, falling back to a build pinned in `_DEFAULT_TARGET_BY_PLATFORM` -- bumped to the promoted build at every stable promotion, and drift-tested against `apps/minds/release-channels.toml` -- when the manifest cannot be read; `source` -> the public GitHub repo; unknown or missing platforms 404). The event is tagged by the usual merge rule: the `imbue_attribution` cookie supplies the visitor id and first touch, and campaign params on the `/download` URL itself overwrite the last touch (or synthesize the sole touch when the cookie is absent); the redirect always happens even when the event write fails.

New accounts are additionally stamped with marketing attribution at creation time (never on sign-in), on both browser signup paths (email-password and Google OAuth): one write-once `account_attribution` row per account, built from the `imbue_attribution` cookie (set server-side by imbue.com's edge function on `.imbue.com`) merged with the signup page's own campaign params. Capture fails open, so it can never break a signup. The cookie's schema, set/update rules, and the download/signup link formats are pinned in [docs/attribution-cookie-contract.md](docs/attribution-cookie-contract.md); the connector-side logic lives in `attribution.py`, the tables come from `migrations/026_account_attribution.sql`, and reporting is plain SQL against Neon (no admin surface).

### Auth

These endpoints front the SuperTokens core so that clients (e.g. the `minds` desktop client) never need the SuperTokens API key. They require `SUPERTOKENS_CONNECTION_URI` (and usually `SUPERTOKENS_API_KEY`) to be configured on the server; otherwise they return 503. All of them are unauthenticated *except* `/auth/session/revoke`, `/auth/session/revoke-current`, `/auth/email/send-verification`, and `/auth/email/is-verified`, which must be called with the caller's own access token (see below); those deliberately accept a session whose email is not yet verified.

#### Deprecated JSON auth endpoints

`POST /auth/signup` and `POST /auth/signin` are **deprecated** in favor of the browser-based accounts surface (consumed by `mngr imbue_cloud auth login`). Account **creation** through the JSON API is disabled on production and staging: `POST /auth/signup` there answers status `SIGNUP_DISABLED` with a message pointing at the browser flow, so every new account goes through the Turnstile-gated web signup (or Google). Dev/CI tiers keep the headless JSON signup because it makes testing easy, and the admin-key `POST /admin/test-signup` endpoint covers deployment tests everywhere. Sign-in stays available on every tier. The old JSON OAuth pair (`/auth/oauth/authorize` + `/auth/oauth/callback`) is **removed** -- Google sign-in exists only as the accounts surface's browser flow. The `needs_email_verification` response field is pinned `false` for wire compat -- verification is non-blocking, and old clients treat `true` as "blocked pending verification".

- `POST /auth/signup` (deprecated; disabled on production/staging, see above) -- Body: `{email, password}`. Returns status, user info, and session tokens. No verification email is sent at signup (the first verification-gated action triggers a contextual send). Refused with status `ACCOUNT_EXISTS_WITH_OTHER_METHOD` when the email is already registered under a different login method (e.g. Google), and with status `SIGNUP_DISABLED` on the restricted tiers.
- `POST /auth/signin` (deprecated, see above) -- Body: `{email, password}`. Returns status, user info, and session tokens; an unverified email does not block sign-in.
- `POST /auth/session/refresh` -- Body: `{refresh_token}`. Returns a new access/refresh token pair.
- `POST /auth/session/revoke` -- Header: `Authorization: Bearer <access_token>`. Revokes every SuperTokens session for the caller's user. The user_id is derived from the access token, so an anonymous caller cannot revoke another user's sessions. The hosted account page's "sign out of all devices" action.
- `POST /auth/session/revoke-current` -- Header: `Authorization: Bearer <access_token>`. Revokes only the presented session. Called on desktop sign-out so signing out of one install does not kill the user's browser session or other devices.
- `POST /admin/test-signup` -- Admin-key authenticated (like `/admin/accounts/*`). Body: `{email, password, verified}`. Creates a test account, optionally pre-verified (the only path that marks an email verified without a clicked link); used by the deployment tests.
- `POST /auth/email/send-verification` -- Header: `Authorization: Bearer <access_token>`. Body: `{email}`; the email must belong to the authenticated caller (403 otherwise). Sends the verification email; returns `{status, sent}` where `sent` is false when the per-user cooldown suppressed the send.
- `POST /auth/email/is-verified` -- Header: `Authorization: Bearer <access_token>`. Body: `{email}`; the email must belong to the authenticated caller (403 otherwise). Returns `{verified: bool}`.
- `GET /auth/verify-email?token=...` -- Serves the hosted accounts bundle's verify-email result page (the page consumes the token via `POST /accounts/api/verify-email`). Used by the link inside verification emails.
- `POST /auth/password/forgot` -- Body: `{email}`. Always returns OK (to avoid account enumeration).
- `POST /auth/password/reset` -- Body: `{token, new_password}`. Consumes a reset token and sets a new password.
- `GET /auth/reset-password?token=...` -- Serves the hosted accounts bundle's password-reset form (the form posts to `POST /auth/password/reset`). Used by the link inside password-reset emails.
- `GET /auth/users/{user_id}` -- Returns basic info about a user (email, login provider).
