# Self-hosted error tracking for minds remote services (Bugsink)

## Overview

The minds remote services currently report errors nowhere: an unhandled exception in the remote service connector, the LiteLLM proxy, or the OAuth redirector is visible only in Modal function logs.
This spec adds error tracking for those services using [Bugsink](https://www.bugsink.com/), a self-hosted, Sentry-SDK-compatible error tracker, with one isolated instance per tier.

Self-hosting was chosen over Sentry SaaS deliberately: production events can carry information (user emails, host ids, log fragments) that must not flow into a company-wide-accessible third-party app.
With Bugsink on a per-tier VPS + Neon, event data only ever touches parties that already hold each tier's production data.

Hosting converges on the OpenObserve pattern established by `specs/minds-openobserve-telemetry.md` (decided in [mngr-internal#464](https://github.com/imbue-ai/mngr-internal/issues/464) before anything was deployed): a small per-tier OVH VPS whose Cloudflare-proxied ingest hostname exposes ONLY the Sentry-protocol DSN routes, with the UI reachable exclusively over an SSH tunnel -- no public login page, error tracking that stays up during a Modal incident, one operational pattern for both observability systems, and a cheaper always-on footprint than a warm Modal container.

The application-level claims in this spec were verified by a working prototype of an earlier Modal-hosted revision (against a Neon Postgres database): migrations and steady-state operation on Postgres, eager-mode digestion, retention behavior, concurrency safety, SDK compatibility with our pinned `sentry-sdk` (2.59.0 at the time; since bumped, see below), API-driven provisioning, and failure modes (dead backend, process kill, redeploy). The VPS-hosting specifics (ingest path shapes through the caddy gate, tunneled-UI behavior) are pinned best-effort in code and confirmed by the dev bring-up's validation gates (see `apps/minds/docs/deploy/setup/bugsink.md`).

### In scope

- `apps/remote_service_connector` (FastAPI web app + its cron/spawned Modal functions).
- `apps/modal_litellm` (the LiteLLM proxy + its `migrate_db` function).
- `apps/oauth_redirector` (dev/CI tiers only, matching where that app is deployed).
- The Bugsink instance tooling in `apps/observability` (sharing the OpenObserve instances' VPS machinery) and a shared SDK-init helper in `libs/modal_app_kit`.

### Out of scope

- Workspace (default-workspace-template) services: workspaces are the user's space; error reporting there needs user consent plumbing and is a separate project.
- Share relays (`frps`, upstream Go binary; failures already surface via the connector's `relay_health_sweep`), owner-exec (separate Go repo), and apt_mirror (Cloudflare Worker, single global instance).
- Alerting integrations (email/Slack/webhooks): deliberately none, see "Alerting" below.
- Region-pinning the connector and LiteLLM proxy themselves near their Neon DBs (tracked separately; see "Future work").

## Key decisions (settled)

| Decision | Choice |
|---|---|
| Error tracker | Bugsink `2.5.0` (pip package, ==-pinned), PolyForm Shield license (free for non-competing use; accepted) |
| Isolation | One instance per tier: `production`, `staging`, and one shared `dev` instance serving all dev/CI envs |
| Hosting | One small OVH Public Cloud VPS per tier (its own box, separate from the tier's OpenObserve VPS) + Neon Postgres, operator-lifecycle via `just provision-bugsink`; VPS region chosen near the tier's Neon |
| Ingress | Cloudflare-proxied `errors.<tier domain>` exposing ONLY the Sentry-protocol DSN ingest routes; UI / REST API loopback-only over SSH (the OpenObserve split-plane model) |
| Task processing | `TASK_ALWAYS_EAGER=True` (no snappea foreman); retention is digest-time-driven so nothing needs a scheduler |
| Secrets and DSNs | HCP Vault per tier (never committed; the repo is public-mirrored and DSNs would invite event spam) |
| SDK wiring | Plain `sentry_sdk.init` via a shared `modal_app_kit` helper; LiteLLM additionally uses its native `failure_callback=["sentry"]` |
| Storm protection | Per-project quotas + a client-side dedup/rate-limit `before_send` hook (slim port of `imbue_common`'s) |
| Alerting | None; separate inspector services will periodically poll the instances via their REST APIs |
| CI envs | Report to the shared dev instance, distinguished by the `environment` tag; env-var kill switch available |
| Telemetry | `PHONEHOME=false` on every instance |

## Architecture

### Instances and tiers

Every instance is operator-lifecycle, provisioned and replaced by operator-invoked `just` recipes -- like the share relays and the OpenObserve instances, and deliberately OUTSIDE `minds-admin env deploy`/`destroy`: error history intentionally survives env re-deploys and destroys, and a `minds-admin env destroy --yes-i-mean-staging` does not touch the staging instance or its database.

**production / staging**: one instance each, in an OVH region near the tier's Neon project, whose `bugsink` database is created by hand inside the tier's existing Neon project.

**dev/CI**: one shared dev instance, treated as tier-level shared infrastructure (like the dev SuperTokens core and the shared dev OpenObserve instance), with its own small dedicated Neon project in the dev Neon org.
All `dev-*` and `ci-*` envs report to it; the `environment` tag keeps them distinguishable.
Note that the ci tier has its own Vault prefix (`secrets/minds/ci`), so its `sentry` entry must be populated with the SAME dev-instance DSNs as `secrets/minds/dev/sentry` -- the provisioning flow writes both (otherwise ci deploys would silently get empty DSNs and never report, contradicting the decision that CI reports).
Per-dev-env deploys must not gain a dependency on the instance: a missing or empty DSN simply disables the SDK in the consuming service.

### The VPS host (`apps/observability`)

The instance tooling lives in `apps/observability` alongside (and sharing machinery with) the OpenObserve instance tooling -- the OVH provisioner, the caddy-fronted split-plane ingress, the origin firewall, the SSH deploy, and the proxied-DNS upsert are one code path for both systems. Bugsink-specific pieces:

- `deploy_assets/bugsink_conf.py`: our vendored Django settings module, derived from bugsink's own `docker.py.template` with exactly one functional delta: `SNAPPEA["TASK_ALWAYS_EAGER"] = True`. Everything else stays as upstream wrote it (env-driven), including the `WORKAHOLIC`/`NUM_WORKERS`/`PID_FILE` snappea knobs -- irrelevant and never consulted in eager mode, but kept verbatim so re-diffing against upstream stays clean. A comment records the upstream template version it was derived from; every version bump re-diffs against the new upstream template.
- `deploy_assets/bugsink_requirements.txt`: the committed hash-locked pip set (`bugsink==2.5.0` + `psycopg2-binary`, every transitive ==-pinned with sha256 hashes, compiled under the repo's supply-chain cooldown -- see `bugsink_requirements.in` for the regen command). The host installs it into `/opt/bugsink/venv` with `--require-hashes`, digest-stamped so a re-deploy with unchanged pins skips the install.
- The systemd unit (in `deploy_assets/cloud-init-bugsink.yaml`): `ExecStartPre` runs `bugsink-manage migrate` then `bugsink-manage prestart` (which handles `CREATE_SUPERUSER` idempotently), then `gunicorn --workers=1 --threads=8 ... bugsink.wsgi` bound to loopback. Exactly one instance and one gunicorn worker, always: Bugsink is a single-writer design; request concurrency comes from threads.
- The caddy gate (`bugsink_render.py`): TLS Full (strict) with the tier's Cloudflare origin certificate; only `/api/<project_id>/envelope/` and `/api/<project_id>/store/` (the Sentry-protocol DSN routes) are proxied, everything else -- login page, UI, canonical REST API -- answers 404 publicly and is reachable only via `ssh -L` to loopback. The origin firewall admits 443 from Cloudflare's published ranges only.
- The operator escape hatch: any `bugsink-manage` subcommand runs over SSH from the instance's EnvironmentFile environment (the provisioning flow mints its REST API token this way).

**Concurrency model** (validated empirically): digestion correctness on Postgres does not depend on any in-process lock.
Bugsink's in-process semaphore and `BEGIN IMMEDIATE` machinery are sqlite-only; on Postgres, concurrent digests serialize on ordinary row locks against the shared per-hour usage-counter rows (whose increments are atomic `F()` expressions).
This was verified with 40 simultaneous new-issue digests: zero errors, unique `digest_order`s, exact counters.
We still run one instance and one gunicorn worker because that is upstream's stated "single-writer architecture" and there is no throughput reason to deviate.
Known residual quirk: creating a new per-hour counter bucket row has no concurrency handling (unique constraints backstop it), so at worst one event per hour boundary can 500 under concurrent load; this is accepted.

**Region choice**: each instance's VPS is provisioned in the OVH region nearest its tier's Neon project (the `ovh_region` argument of `just provision-bugsink`; dev: `US-WEST-OR-1` for the us-west-2 Neon org).
The digest transaction issues ~70 sequential queries, so throughput is approximately `1 / (70 x RTT)`: measured ~0.5 events/s at ~26ms RTT during the Modal-era prototype, and ~1.4 events/s at the ~10ms TCP-connect RTT measured on the dev bring-up (OVH US-WEST-OR-1 -> Neon aws-us-west-2); ~30/s co-located matches upstream's published number.

### Secrets and configuration

Two new Vault-backed services, following the existing `.minds/template/<service>.sh` schema convention:

**`.minds/template/bugsink.sh`** -> Vault `secrets/minds/<tier>/bugsink`. Operator-only, like `observability`: read by the `just provision-bugsink` recipes, never pushed to Modal by any deploy:

```sh
export SECRET_KEY=               # Django secret key, >= 50 random chars
export DATABASE_URL=             # postgres DSN, DIRECT (non -pooler) Neon host
export CREATE_SUPERUSER=         # email:password, consumed on first boot only; the break-glass account
export BUGSINK_API_TOKEN=        # bearer token for the (loopback-only) REST API. NOT consumed by the
                                 # instance itself -- minted by provisioning (empty until then), read by
                                 # inspector services
export BUGSINK_SSH_PRIVATE_KEY=  # dedicated per-tier keypair for the instance VPS
export BUGSINK_SSH_PUBLIC_KEY=
export BUGSINK_ORIGIN_TLS_CERT=  # Cloudflare origin certificate (PEM) + key caddy terminates TLS
export BUGSINK_ORIGIN_TLS_KEY=   # with, covering errors.<tier cloudflare_domain> (Full (strict))
```

There is no `BASE_URL` key: the public ingest hostname derives from the tier deploy.toml's `cloudflare_domain` (`errors.<domain>`), and Django's `BASE_URL` (which the project DSNs embed) is rendered from it.

**`.minds/template/sentry.sh`** -> Vault `secrets/minds/<tier>/sentry` -> Modal secret `sentry-<tier>-<deploy_id>`, consumed by the reporting services:

```sh
export RSC_SENTRY_DSN=              # connector project DSN on the tier's bugsink
export LITELLM_SENTRY_DSN=          # litellm-proxy project DSN
export OAUTH_REDIRECTOR_SENTRY_DSN= # oauth-redirector project DSN (dev tier only; empty elsewhere)
```

The service name is `sentry` (not `bugsink`) on the consumer side because the values are Sentry-protocol DSNs and the consuming code is `sentry_sdk`; the backend behind the DSN is an implementation detail to the consumers.
Empty values are permitted everywhere (the deploy filters empty values out, and the SDK helper treats a missing DSN as "reporting disabled"), so tiers bring up in any order and consumers degrade gracefully.

Additional non-secret config (existing mechanisms):

- `MINDS_ENV_NAME` (already in the `litellm-connector` secret) and `MNGR_DEPLOY_ENV` / `MINDS_DEPLOY_ID` (already in `deploy_metadata_secret`) supply the `environment` and `release` values for events on the consumer side.
- The instance's fixed operational values (`PHONEHOME=false`, `BEHIND_HTTPS_PROXY=True`, `MAX_EVENT_AGE_DAYS`, `ALLOWED_HOSTS` admitting the tunneled loopback) are rendered into its EnvironmentFile by `bugsink_render.py`.
- Tier `deploy.toml` additions: `sentry` in `[secrets].services` (the only deploy-coupled piece).

### Bring-up ordering and provisioning

Bugsink bring-up is an explicit, earlier step per tier; in the steady state there is no ordering constraint. The operator runbook is `apps/minds/docs/deploy/setup/bugsink.md`; the shape:

1. Create the backing resources (the `bugsink` Neon database, the origin certificate for `errors.<domain>`, the SSH keypair, the break-glass credentials) and populate `secrets/minds/<tier>/bugsink` in Vault; push the tier's `sentry` entry all-empty.
2. `just provision-bugsink <ovh-region>`: orders the VPS, installs the hash-locked venv + ingest gate (first boot runs migrations and creates the superuser), mints an API auth token (`bugsink-manage create_auth_token` over SSH), creates the team and the per-service projects through Bugsink's REST API (`/api/canonical/0/teams/`, `/projects/` -- driven through an SSH tunnel, since the API is loopback-only), reads each project's DSN from the project detail endpoint, and writes the results into Vault: DSNs into `secrets/minds/<tier>/sentry`, the token into `BUGSINK_API_TOKEN`. For the dev instance it writes the DSNs to both `secrets/minds/dev/sentry` and `secrets/minds/ci/sentry` (see "Instances and tiers"). Then it points the Cloudflare-proxied `errors.<domain>` record at the instance. The token/projects pass is idempotent (get-or-create by name; existing DSNs are preserved) and re-runnable standalone via `just provision-bugsink-projects <ip>`.
3. Re-run the tier's normal deploy so the consumer apps pick up the freshly stamped `sentry-<tier>-<deploy_id>` secret.

Until step 2+3 complete, consumers see an empty DSN and simply do not report -- no failure mode.

**Projects**: one Bugsink project per reporting service per instance: `rsc` and `llm` everywhere, plus `oauth-redirector` on the dev instance only.
Per-project quotas mean one storming service cannot drown the others' events.

**Lifecycle semantics**: the instances are operator-lifecycle; `minds-admin env deploy` / `destroy` never touch them or their databases, and error history deliberately survives env destroys.
Replacement (version bump, OS refresh, dead box) is sequential and single-writer: stop the bugsink service on the old instance FIRST (no quiesce window is needed -- every acked event is already durable in Postgres), provision the replacement with a bumped ordinal (it adopts the same database and resumes with full history and unchanged DSNs), repoint DNS, then `just destroy-bugsink-instance` the old VPS.

## Client integration

### The shared init helper (`libs/modal_app_kit/imbue/modal_app_kit/sentry.py`)

A new module in `modal_app_kit` (already shipped into every Modal container) providing:

- `init_sentry(service_name: str, dsn_env_var: str) -> None`: idempotent per container (guarded like the connector's `_init_supertokens_once`). Reads the DSN from the named env var; a missing/empty value or `MINDS_SENTRY_DISABLED=1` means no-op (the kill switch covers noisy CI runs and emergency shut-off without a Vault edit). Calls `sentry_sdk.init` with: `environment` = `MINDS_ENV_NAME` if set else `MNGR_DEPLOY_ENV`, `release` = `MINDS_DEPLOY_ID`, `server_name` = the service name (also set as a `service` tag, matching the minds client convention), `traces_sample_rate=0.0`, `send_default_pii=False`, `include_local_variables=False`, and the rate-limiting `before_send` below.
- A slim event rate limiter: a self-contained `before_send` reimplementation of `imbue_common`'s `_SentryEventRateLimiter` (per-exception-key dedup with an initial grace count and growing timeout). This is deliberate, flagged duplication: the containers cannot ship `imbue_common` (their deployment model ships only the app package + `modal_app_kit`), and the original is coupled to loguru. The module docstring cross-references the original. This is the crash-storm control: a tight error loop is the same exception repeating, and dedup collapses it client-side before it hits the wire.
- A `capture_and_reraise` helper (context manager) for Modal cron/spawned functions, since Modal owns their top-level exception handling and the SDK's excepthook integration does not see them.

`sentry-sdk` is added (==-pinned to the workspace version, currently `2.66.0`) to the image dependency groups of all three consumer apps.
The pin must stay >= 2.63.0: earlier SDKs re-wrap `dependant.call` in place on every request to a sync FastAPI endpoint served through lazy router inclusion, killing the endpoint with RecursionError after ~990 requests in a warm container ([mngr-internal#493](https://github.com/imbue-ai/mngr-internal/issues/493); regression-tested by the connector's `test_sentry_wrapper_leak.py`).
It is already present transitively in the connector image; pinning makes it explicit and covers the other two.
`sentry_sdk` is added to each app's `THIRD_PARTY_IMPORT_ROOTS`.

### Per-service wiring

**remote_service_connector**: `init_sentry("remote-service-connector", "RSC_SENTRY_DSN")` at the top of `fastapi_app()` and of every cron/spawned function (each wrapped in `capture_and_reraise`).
The stamped `sentry` secret is added to `_connector_secrets()`.
The integrations give us: unhandled request exceptions, stdlib `logger.warning`/`logger.error` calls as events, and INFO-level breadcrumbs.

The reporting policy (added after the bring-up): stdlib log levels ARE the priority scheme. `logger.error` (and any exception escaping to the connector's app-level 500 handler, `http_api.handle_unexpected_exception`) reports at error level -- failures nothing tolerated. `logger.warning` reports at warning level -- exceptions the code caught and continued past for robustness. Expected, routine anomalies (transient upstream errors, client-input junk) are neither: they are counted as `metric` JSON log lines (`imbue.modal_app_kit.metrics`) flowing into the tier's OpenObserve, where a rate change is the signal. Every connector-defined exception inherits `errors.ConnectorError`; the expected set is exactly what `raise_as_http` maps to status codes, and 500 bodies are a generic `internal_error` detail carrying the Bugsink event id (plus the exception repr on dev/ci tiers only -- production and staging never leak exception text to clients).

**modal_litellm**: same init in `litellm_app()` and `migrate_db()`, plus LiteLLM's native integration: `"failure_callback": ["sentry"]` in `LITELLM_CONFIG`'s `litellm_settings`.
LiteLLM's callback reads the literal `SENTRY_DSN` env var, so `litellm_app()` must copy `LITELLM_SENTRY_DSN` into `os.environ["SENTRY_DSN"]` before importing the proxy server (precedent: that function already sets `CONFIG_FILE_PATH`/`WORKER_CONFIG` the same way); our own `sentry_sdk.init` passes the DSN explicitly and is unaffected.
The native callback's failure payloads can include request contents (prompts); this is accepted because the LiteLLM database already stores request metadata and the instance-wide short retention (below) bounds the exposure window.
The stamped `sentry` secret is added to the proxy's secret list.

**oauth_redirector**: same init with `OAUTH_REDIRECTOR_SENTRY_DSN`.
Its deploy has no Vault-backed secrets today; the DSN is threaded at deploy time into its existing inline `modal.Secret.from_dict` from a deploy-environment variable, keeping that app's zero-Vault deployment story.

## Operational policy

### Retention

`MAX_EVENT_AGE_DAYS=30` instance-wide, plus Bugsink's default per-project cap of 10,000 stored events.
Note: this is a deliberate simplification of the earlier "30 days for llm, 90 for the connector" intent -- Bugsink's age-based retention knob is global to the instance, not per-project.
30 days everywhere is chosen because short retention is the compensating control for prompt-bearing LiteLLM failure payloads, and issue rows (titles, counts, first/last-seen) survive event deletion, so the connector loses little.
Age-based deletion and count-based eviction are both digest-time-driven and therefore work under eager mode with no scheduler (verified in source and prototype).

### Quotas and the throughput envelope

Bugsink's built-in limits are kept at their defaults initially (1,000 events per project per 5 minutes, 5,000/hour, 1M/month, same numbers installation-wide) and are env-tunable if they ever bind.
Expected steady-state volume is near zero; the worst realistic sustained source (an every-minute cron crash-looping) is ~0.02 events/s.
Sustained ingest capacity with a near-Neon VPS region is ~1-2 events/s (~10ms cross-provider RTT measured on dev; digestion is installation-wide serialized, and separate projects do not parallelize) -- ~70x the worst realistic sustained source.
Above capacity: requests queue (gunicorn threads), then Bugsink 429s, which the SDK honors by dropping.
The SDK never blocks the reporting service: captures cost <1ms and sending happens on a background thread (verified against a dead backend).

### Access management

- Break-glass superuser credentials per tier live only in Vault (`CREATE_SUPERUSER`).
- Human accounts are created via Bugsink's invite flow; with no email backend configured, the UI displays the invite link for copy-paste, so no SMTP is needed. Initial invites: `josh_production@imbue.com` on the production instance, `josh_staging@imbue.com` on staging.
- Self-registration stays at Bugsink's invite-only default; the Django admin (`USE_ADMIN`) stays off.
- The UI (and the login page) is reachable only over an SSH tunnel to the instance's loopback (`ssh -L 8300:127.0.0.1:8300`); it does not exist on the public surface.
- Machine access for the inspector services is the `BUGSINK_API_TOKEN` bearer token against the REST API -- which is likewise loopback-only, so inspectors will need SSH (or a deliberate, token-authed widening of the caddy gate when they are built).

### Alerting

None, deliberately: no SMTP, and no Slack/webhook backends even though Bugsink ships them, because alert payloads would leak event information into Slack -- the exact exposure this design exists to avoid.
Separate inspector services (out of scope here) will periodically poll each instance's REST API using `BUGSINK_API_TOKEN`.

### CI envs

`ci-*` deploys report to the shared dev instance like any dev env.
This is desirable: deployment tests exercising failure paths (e.g. the broken-healthcheck rollback test) prove the reporting pipeline works end to end.
The noise is bounded by per-project quotas and filterable by the `environment` tag; a test run that must not report sets `MINDS_SENTRY_DISABLED=1`.

### Upgrades and housekeeping

- The bugsink pin is bumped deliberately: bump `bugsink_requirements.in`, recompile the hash-locked export (command in that file), re-diff `bugsink_conf.py` against the new upstream `docker.py.template`, then replace the instance (replace-not-update, like OpenObserve).
- An instance replacement drops whatever the reporting SDKs have in flight at that moment (sending is fire-and-forget by design); this is accepted and is not a bug.
- The optional `vacuum_*` maintenance commands (tag/file table housekeeping) are manual-only tasks in eager mode; they are deferred until needed and runnable via `bugsink-manage` over SSH.
- Health: `observability bugsink deploy` polls the loopback login page over SSH until it answers 200 (the same signal used to validate the prototype; a 200 proves migrations finished and gunicorn serves). The public health signal is a GET of a DSN ingest path answering a Django-generated 4xx (pinned at dev bring-up); the connector's health-sweep probe of it is a follow-up, shared with the OpenObserve `/healthz` probe.
- Bugsink's own `SENTRY_DSN` (self-reporting) stays unset.

## Testing

- Unit tests for `modal_app_kit.sentry`: DSN/env resolution, the kill switch, idempotent init, rate-limiter behavior (grace count, dedup, timeout growth), `capture_and_reraise`.
- Unit tests in `apps/observability` for the pure parts: the renderers (EnvironmentFile escaping, caddy gate surface, allowed hosts), the install script (hash-locked install, file modes, restart ordering, unit/constant agreement), and the REST provisioning (token parsing, get-or-create idempotence, project-to-Vault-key mapping) against an httpx mock transport.
- Conf-vs-template drift is covered by the documented re-diff step (not a test, since upstream's template is not vendored); the hash-locked requirements export is asserted well-formed by a unit test.
- Deployment test (shipped with the reporting-policy conversion): the connector exposes `GET /health/reporting-probe` on dev/ci tiers only -- one call emits a metric log line, logs a warning-level Bugsink event, and raises an unmapped exception through the app-level 500 handler. The `minds_services` test `test_error_reporting.py` drives it and asserts the `internal_error` response contract including a well-formed, non-empty `event_id`, which is the end-to-end proof the DSN plumbing and SDK capture work after any deploy change. Store-side delivery is deliberately not asserted (the REST/query APIs are loopback-only by design); the probe's unique `marker` makes the operator's manual lookup trivial.

## Implementation plan

1. Instance hosting in `apps/observability` (renderers, hash-locked requirements export, cloud-init + unit, remote install, REST provisioning, the `observability bugsink` CLI) + `modal_app_kit.sentry` helper + unit tests.
2. Operator plumbing: the Vault templates, `scripts/provision_bugsink_config.py`, the `just` recipe family, `sentry` in every tier's `[secrets].services`, and the bring-up runbook.
3. Consumer wiring: connector, litellm proxy, oauth_redirector (each a no-op until its tier's `sentry` Vault entry is populated).
4. Tier bring-up (operational, no code): dev instance first with the runbook's validation gates, then staging, then production; invites per "Access management".
5. Follow-up: the connector health-sweep probe and the separately-tracked region-pinning of rsc/llm (the deployment test shipped with the reporting-policy conversion).

## Future work

- Region-pin `rsc-<tier>` and `llm-<tier>` near their Neon databases (they pay the same ~26ms-per-query RTT tax on every request today). Tracked in [imbue-ai/mngr-internal#443](https://github.com/imbue-ai/mngr-internal/issues/443).
- Workspace-service error reporting (consent plumbing + DSN injection via the minds client), if ever desired.
- Inspector services that poll the instances (separate project; this spec only guarantees them an API token -- and the access-path decision noted under "Access management").
- Optionally install the observability collector on the bugsink hosts so their host metrics/journals land in the tier's OpenObserve (needs a sender-class decision; deliberately not part of v1).
