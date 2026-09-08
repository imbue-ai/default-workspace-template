# Log and telemetry aggregation for minds infrastructure (OpenObserve)

## Overview

The minds server fleet currently has no aggregated logs or metrics.
Modal function logs (the connector, the LiteLLM proxy, the OAuth redirector) are visible only in the Modal dashboard, with Modal's retention.
The bare-metal boxes that host workspace slices and the OVH VPS share relays have no health metrics or retained logs at all, so a full disk or thrashing box surfaces only as mysterious workspace failures.

This spec adds one self-hosted [OpenObserve](https://github.com/openobserve/openobserve) instance per tier -- a single-binary, OTLP-native logs/metrics/traces store -- running on a small per-tier OVH VPS, with bulk data in the tier's Cloudflare R2 and metadata in the tier's Neon Postgres.
Modal-side telemetry arrives with zero application-code changes via Modal's workspace-level OpenTelemetry integration; box and relay telemetry arrives from a pinned OpenTelemetry Collector installed by the existing server-prep and relay-provisioning flows.

Self-hosting was chosen over SaaS observability for the same reason the Bugsink error tracker (see `specs/minds-bugsink-error-tracking.md`, branch `mngr/env-tier-sentry`) rejected Sentry SaaS: production logs carry information (user emails, host ids, request paths) that must not flow into a company-wide-accessible third-party app.

OpenObserve complements Bugsink; it does not replace it.
Bugsink owns error tracking (issue grouping, dedup, per-project quotas); OpenObserve owns raw logs and metrics.
Bugsink's own Modal function logs flow to OpenObserve like any other app's, which is desirable (each system helps debug the other).

### In scope

- One OpenObserve instance per tier: `production`, `staging`, and one shared `dev` instance serving all dev/CI envs.
- Modal workspace telemetry (function logs, container metrics, audit logs) for every Modal app in each tier's workspace.
- Host metrics and system logs from the bare-metal slice boxes and the share-relay VPSes.
- The provisioning recipes, Vault schema, Cloudflare ingress fronting, and fleet collector rollout.

### Out of scope

- Workspace (default-workspace-template) internals and the desktop client: user space; telemetry there needs consent plumbing and is a separate project.
- **No collector inside the lima VMs, ever.** Per-slice visibility comes from box-level qemu process metrics only.
- Error tracking (owned by Bugsink) and the future migration of Bugsink onto this hosting pattern ([mngr-internal#464](https://github.com/imbue-ai/mngr-internal/issues/464)).
- Dashboards and alert rules (a separate PR builds on the deployed instances).
- Alerting integrations (email/Slack/webhooks): deliberately none, see "Alerting" below.
- The apt_mirror Cloudflare Worker (single global instance; Logpush is possible future work).

## Key decisions (settled)

| Decision | Choice |
|---|---|
| Backend | OpenObserve, ==-pinned release, AGPL-3.0 (unmodified internal self-hosting; license accepted, matching the Bugsink PolyForm Shield precedent) |
| Isolation | One instance per tier: `production`, `staging`, and one shared `dev` instance for all dev/CI envs |
| Hosting | Small OVH VPS per tier (relay-style provisioning), NOT Modal: OpenObserve is a disk-first single-writer store, and a real persistent disk eliminates the container-recycling durability problem outright |
| Storage | Parquet data in a per-tier R2 bucket (`ZO_LOCAL_MODE_STORAGE=s3`); metadata in Neon Postgres (`ZO_META_STORE=postgres`); only a ~1-minute WAL buffer lives on the VPS disk, so the box is disposable |
| Ingress | One Cloudflare-proxied hostname per tier exposing ONLY the OTLP ingest routes and `/healthz`; origin firewall accepts only Cloudflare IP ranges plus SSH |
| Web UI | Never exposed publicly; reached via SSH port-forward only (operator SSH keys already live in Vault); no Tailscale, no Cloudflare Access |
| Modal-side collection | Modal's workspace-level OpenTelemetry integration pushing to the tier's ingest hostname; zero app-code changes |
| Fleet-side collection | Pinned `otelcol-contrib` (hostmetrics + journald) as a systemd service on every bare-metal box and relay VPS, installed by the existing prep/provision flows |
| Auth | Distinct ingest credentials per sender class (Modal, boxes, relays) so any leak is rotated independently; secrets in HCP Vault |
| Retention | Metrics 25 months; logs 90 days |
| Single-writer | Exactly one instance per tier's storage, always; replacement is sequential stop-then-start, never overlapping |
| Alerting | None; consistent with the Bugsink spec's rationale (alert payloads would leak event data) |

## Architecture

### Instances and tiers

**production / staging**: one instance each, provisioned and destroyed by operator-invoked `just` recipes.
The instances are deliberately OUTSIDE the `minds env deploy` / `destroy` lifecycle, like the share relays and the shared `bugsink-dev` instance: telemetry history intentionally survives env re-deploys, and `minds env destroy --yes-i-mean-staging` does not touch the staging instance, its R2 bucket, or its metadata database.

**dev/CI**: one shared instance, treated as tier-level shared infrastructure.
All `dev-*` and `ci-*` envs report to it automatically, because the Modal integration is configured per Modal *workspace* and every dev/CI env deploys into the shared `minds-dev` workspace.
There is consequently no per-env opt-out for Modal-side telemetry; CI noise is accepted and filterable by Modal's `app_id` / `function_name` attributes.
Per-dev-env deploys gain no dependency on the instance: if it is down or absent, senders buffer and drop, and nothing else breaks.

Being outside Modal is also a feature in itself: the aggregator keeps receiving box and relay telemetry during a Modal incident, and records the Modal-side gap instead of disappearing with it.

### The VPS host

One small OVH instance per tier (2 vCPU / 4 GB RAM / default disk is ample; the disk holds only the WAL buffer and query cache), in an OVH region near the tier's Neon project.
Provisioning mirrors the share-relay flow (`just provision-dev-relay` / `scripts/provision_dev_relay_config.py`): order the instance via the tier's OVH credentials, install pinned binaries and configs over SSH, reconcile DNS.

The host runs exactly three services under systemd:

- **openobserve** (pinned release binary), bound to `127.0.0.1:5080`.
- **caddy** (the Debian package, kept current by `unattended-upgrades`; its service user is what the deploy grants the origin TLS material to), the TLS terminator and path gate on `0.0.0.0:443` (see "Ingress" below).
- **otelcol-contrib** (pinned release), monitoring the instance's own host, pushing to `127.0.0.1:5080` directly.

Plus the standard OS hygiene from the relay flow: dedicated per-tier SSH keypair (stored in Vault, like the relay key), `ufw`/nftables default-deny with 443 restricted to Cloudflare IP ranges and 22 open for operators, and `unattended-upgrades` for OS patches.

**Upgrades are replace-not-update.**
The OpenObserve (or otelcol) pin is bumped by provisioning a fresh instance and retiring the old one -- see "Replacing an instance" below.
There is no in-place upgrade path to maintain.

### Storage and durability

OpenObserve runs in single-node local mode with remote storage:

- `ZO_LOCAL_MODE_STORAGE=s3` pointed at a per-tier R2 bucket (e.g. `minds-observability-<tier>`) in the tier's existing Cloudflare account, using a dedicated account-owned R2 token scoped to that bucket.
- `ZO_META_STORE=postgres` pointed at an `openobserve` database inside the tier's existing Neon project (production / staging), or a small dedicated Neon project in the dev org for the shared dev instance (mirroring the `bugsink-dev` pattern).
- The local data directory holds only the write path: memtable flushes to local WAL every `ZO_MEM_PERSIST_INTERVAL` (default 5s), and WAL parquet moves to R2 after `ZO_MAX_FILE_RETENTION_TIME`, which we lower from the 600s default to 60s so the disk-only window stays ~1 minute.

Durability envelope: acked data is on local disk within ~5 seconds and on R2 within ~1-2 minutes.
A hard host loss forfeits at most that window; a graceful stop forfeits nothing after a short quiesce.
This is telemetry, not billing data; that envelope is accepted.

Because data and metadata live off-box, the VPS itself is disposable: a replacement instance pointed at the same R2 bucket and metadata database resumes with full history.

### Ingress: the split-plane model

There are two fundamentally different surfaces, and they get different treatment:

**Machine ingest (public, narrow).**
Modal's OpenTelemetry exporter runs in Modal's infrastructure -- not in our containers -- so it can only push to a reachable HTTPS URL with custom headers; it cannot join a VPN, present client certificates, or come from allowlistable IPs.
A public ingest endpoint is therefore unavoidable, and the design concentrates hardening there instead of pretending otherwise:

- One Cloudflare-proxied hostname per tier, `telemetry.<tier cloudflare_domain>` (e.g. `telemetry.minds-dev.com` for the shared dev instance), orange-clouded on the tier's existing zone.
- TLS Full (strict) with a Cloudflare origin certificate on caddy.
- Caddy allows ONLY the OTLP HTTP ingest routes (`/api/<org>/v1/logs`, `/v1/metrics`, `/v1/traces`; exact shapes pinned during the prototype) plus an unauthenticated `GET /healthz`; caddy answers 404 for every other path, so the UI, query API, and admin surface do not exist publicly.
- The origin firewall accepts 443 only from Cloudflare's published IP ranges, so the origin cannot be reached by scanning around the proxy.
- Every ingest request carries a per-sender-class credential (see "Secrets" below).

**Human access (private, SSH-only).**
Operators reach the UI and query API via `ssh -L 5080:127.0.0.1:5080 <instance>` and browse `http://localhost:5080`.
The SSH key is already the operator credential of record for tier hosts (Vault-held), UI use is expected to be rare until the dashboards PR, and this keeps the instance's attack surface to exactly: SSH, and token-authenticated OTLP POSTs via Cloudflare.
No Tailscale: it would add a cross-tier shared plane and a node on infrastructure hosts for a door SSH already provides.

## Telemetry sources

### Modal workspace integration

Configured once per Modal workspace (production, staging, `minds-dev`) in the workspace observability settings:

- Endpoint: the tier's bare `https://telemetry.<domain>` base URL; caddy rewrites the appended `/v1/*` suffixes onto OpenObserve's `/api/<org>` routes and stamps `stream-name: modal_logs` there (Modal is the only bare-path sender; the header cannot ride in the secret because Modal Secret keys must be valid environment variable names, which excludes the hyphenated `stream-name`).
- Auth: a Modal Secret (`observability-otel-ingest`, CLI-creatable) carrying only `OTEL_HEADER_Authorization` (the Modal sender credential from Vault).

This forwards, with zero code changes in any app: function logs (including the connector's `RequestLoggingMiddleware` access lines and everything the LiteLLM proxy and Bugsink print), Modal's container metrics (CPU, memory, coldstarts, queue depths, tagged with `function_name` / `app_id`), and workspace audit logs.
Uninstalling the integration in the workspace settings is the kill switch.

**Note:** this integration is the design's single biggest external dependency and is prototype item number one (see "Prototype validation plan").

### Bare-metal boxes and share relays

A pinned `otelcol-contrib` systemd service on every box and relay VPS:

- Receivers: `hostmetrics` (cpu, load, memory, disk, filesystem, network, and the `process` scraper filtered to the interesting processes -- `qemu-*` for per-slice CPU/memory visibility from outside the VMs, `frps` on relays, `sshd`) and `journald` (sshd auth events, kernel/OOM messages, and the collector's own unit).
- Processors: `memory_limiter` with a hard cap (the collector must never compete with customer slices for memory), `batch`, and `resource` attributes stamping tier and role (`box` / `relay`); the machine itself is identified by the hostname `resourcedetection` stamps.
- Exporter: `otlphttp` to the tier's `https://telemetry.<domain>` with the box/relay sender credential, and a `file_storage`-backed sending queue so instance downtime (e.g. during a replacement) buffers on the sender instead of dropping.

Install paths reuse existing, idempotent flows:

- Boxes: a step in `mngr imbue_cloud admin server prep` (so `just prep-server <id>` rolls it to existing boxes, and new boxes get it automatically).
- Relays: a step in the relay provisioning script (`scripts/provision_dev_relay_config.py` and the production relay flow).

The credential is read from Vault at prep/provision time by the operator-side flow and written into the collector config on the machine; rotation is a Vault update plus a re-prep pass.

### The instance's own host

The instance's local `otelcol-contrib` pushes its host metrics and journal to `127.0.0.1:5080` directly (no Cloudflare round-trip, no token exposure), so the aggregator's own disk, memory, and service health are visible in itself.
There is no self-ingestion loop concern on a VPS: unlike the earlier Modal-hosted design, the instance's own stdout is not inside any forwarding pipeline.

## Streams and retention

One OpenObserve organization per instance (the tier IS the isolation boundary; the default org suffices).
Streams are split by source class so retention and queries are per-class -- indicatively `modal_logs`, `modal_metrics`, `modal_audit`, `box_logs`, `box_metrics`, `relay_logs`, `relay_metrics`, `self_*` -- with final names and the OTLP-to-stream routing mechanism pinned during the prototype.

Retention, set per stream class by the provisioning script:

- **Metrics: 25 months** (capacity and seasonality trends; not user-identifying; storage cost on R2 is immaterial at our volume).
- **Logs: 90 days** (log lines can carry user-identifying data such as emails, host ids, and request paths; 90 days is the debugging-friendly starting point while the system beds in, and can be tightened later).

## Secrets and configuration

One new Vault-backed service following the `.minds/template/<service>.sh` schema convention:

**`.minds/template/observability.sh`** -> Vault `secrets/minds/<tier>/observability`:

```sh
export OPENOBSERVE_ROOT_EMAIL=            # break-glass root account (first-boot creation)
export OPENOBSERVE_ROOT_PASSWORD=
export OPENOBSERVE_META_DSN=              # postgres DSN for the metadata DB (direct, non -pooler host)
export OPENOBSERVE_R2_BUCKET=             # per-tier bucket name
export OPENOBSERVE_R2_ACCESS_KEY_ID=      # bucket-scoped account-owned R2 token
export OPENOBSERVE_R2_SECRET_ACCESS_KEY=
export OBSERVABILITY_SSH_PRIVATE_KEY=     # dedicated per-tier keypair for the instance host
export OBSERVABILITY_SSH_PUBLIC_KEY=
export OBSERVABILITY_ORIGIN_TLS_CERT=     # Cloudflare origin certificate (PEM) + key caddy
export OBSERVABILITY_ORIGIN_TLS_KEY=      # terminates TLS with (Full (strict) behind the proxy)
export INGEST_CREDENTIAL_MODAL=           # per-sender-class ingest credentials; minted by
export INGEST_CREDENTIAL_BOXES=           # provisioning after first boot (empty until then)
export INGEST_CREDENTIAL_RELAYS=
```

Each ingest credential is a complete Authorization header value (`Basic <base64(email:password)>`) for a dedicated per-sender-class OpenObserve user, so any leak is rotated independently. Validated on the dev instance (v0.92.2): the OSS release accepts exactly two org roles -- `admin` and `service_account` ("Custom roles not allowed" for everything else) -- so sender users are minted as `service_account`, the least-privileged ingest identity, whose Basic email:password credential ingests OTLP successfully. The release also enforces a password complexity policy (at least one lowercase letter, uppercase letter, digit, and special character), which the minting code guarantees.

Unlike the Bugsink `sentry` entry, this entry is consumed by NO deployed service: it is never listed in any tier's `deploy.toml` `[secrets].services` and never pushed to Modal as a stamped secret.
Its only readers are the operator-side provisioning and prep flows, plus the one hand-configured Modal workspace secret (`OTEL_HEADER_Authorization`).
For the same reason, no `ci`-tier Vault mirroring is needed: ci envs deploy into the shared `minds-dev` workspace, whose workspace-level integration already covers them.

Provisioning (idempotent, mirroring `just provision-bugsink`):

1. Operator populates the schema's operator-owned keys (root account, DSN after creating the metadata DB, R2 token after creating the bucket, SSH key).
2. `just provision-observability` (the tier is derived from the activated env) orders and installs a NEW instance, then mints the three sender credentials via OpenObserve's API (get-or-create), writes them back to Vault, and applies the stream retention settings. Re-running the credential/retention pass against an existing instance is `just provision-observability-accounts <ip>` -- re-running `provision-observability` always provisions another VPS.
3. The Modal workspace integration is configured by hand once per workspace from the Vault value (a documented step in the recipe output; Modal has no API for this today).

Non-secret instance config (`ZO_*` environment, caddy routes, collector configs) is rendered by the provisioning script from committed templates; the OpenObserve and otelcol version pins live in the script alongside them.

## Operational policy

### Single-writer and replacing an instance

Exactly one OpenObserve process may ever run against a given R2 bucket + metadata database.
Replacement (version bump, OS refresh, dead box) is sequential:

1. On the old instance: stop caddy (senders start buffering/retrying), wait one quiesce interval (>= 2x `ZO_MAX_FILE_RETENTION_TIME`, i.e. ~2 minutes) for the WAL tail to land on R2, then stop openobserve.
2. Provision the replacement (`just provision-observability` with a bumped ordinal): the deploy installs AND starts openobserve + caddy -- the new instance adopts the bucket and metadata and resumes with full history -- and repoints the Cloudflare record for `telemetry.<domain>` at the new IP.
3. Destroy the old instance (`just destroy-observability-instance`).

Quiescing the old instance strictly BEFORE provisioning the replacement is what preserves the single-writer invariant: the provisioning flow starts openobserve as part of the deploy, so the old writer must already be stopped by then.

During the swap window (which includes the OVH provisioning time), box/relay collectors lose nothing (file-backed queues); Modal's exporter retries per its own policy, so a small gap in Modal-side telemetry is possible and accepted (its buffering behavior is a prototype observation, not something we control).

### Health: who watches the watcher

- The connector's existing health-sweep pattern gains a probe of each tier's `GET https://telemetry.<domain>/healthz`, logging an error on failure -- which the sentry SDK then lands in Bugsink, closing the loop where each observability system watches the other. (This lands after the Bugsink PR merges; until then the probe just logs.)
- The instance's self-monitoring (local collector) covers disk pressure and service restarts once dashboards exist.
- Systemd `Restart=always` handles process crashes; OVH handles the hardware.

### Alerting

None, deliberately, matching the Bugsink spec: no SMTP and no webhook integrations, because alert payloads would leak log content into third-party surfaces.
The health sweep plus the future dashboards/inspector work cover operational awareness.
Revisit only with a payload-free alert design (an alert may say which rule fired on which stream, and nothing more).

### Cost

Per tier: one small OVH VPS (~$5-15/month), R2 storage (cents at our volume, zero egress), one Neon database in an existing project.
Three tiers total; strictly cheaper than one always-on Modal container per tier.

## Prototype validation plan

This spec precedes its prototype (unlike the Bugsink spec, which was written after one); implementation step 1 is to validate every load-bearing claim on the shared dev tier, specifically:

1. **Modal integration end-to-end**: available on our workspaces/plan; function logs, container metrics, and audit logs actually arrive; `OTEL_HEADER_*` auth works; the endpoint URL shape Modal expects maps onto OpenObserve's OTLP paths; observed exporter behavior while the endpoint is down (retry window, drop behavior).
2. **Storage mode**: single-node local mode with `ZO_META_STORE=postgres` (Neon) + `ZO_LOCAL_MODE_STORAGE=s3` (R2) is stable; kill -9 and clean-stop recovery match the durability envelope above; a replacement instance adopts the storage cleanly.
3. **Ingress**: Cloudflare-proxied OTLP POSTs through caddy's path gate, with sender credentials, from both a real box collector and the Modal integration; `/healthz` reachable; UI genuinely absent from the public surface.
4. **Streams and retention**: OTLP-to-stream routing per source class; per-stream retention overrides applied via API.
5. **Collector on a real box**: hostmetrics + journald + qemu process scraping on an actual slice box, memory cap honored, file-backed queue behavior across an instance restart.

Failures here feed back into this spec before the provisioning work is built out.

## Code layout

No new Modal app; the operator tooling is a dedicated `apps/observability` project mirroring `apps/share_relay` (the established shape for "operator CLI that renders config and drives an OVH VPS lifecycle"), so the config renderers stay pure and unit-testable and the OVH provisioner is reused rather than duplicated:

- `apps/observability` (`imbue.observability`, console script `observability`): primitives, config renderers (openobserve env / Caddyfile / nftables / collector configs), the pinned-and-checksummed install scripts, the SSH deploy, the proxied-DNS upsert, and the OpenObserve API provisioning (sender users + retention) behind a small interface. Depends on `share-relay` for the OVH Public Cloud provisioner.
- `scripts/provision_observability_config.py`: the Vault glue (mirroring `scripts/provision_dev_relay_config.py`): resolves the tier's entries into a work dir for the recipes, renders the fleet collector env (with a distinct "not configured" exit code so recipes skip gracefully), and writes minted credentials back to Vault.
- `private.just`: `provision-observability`, `provision-observability-accounts`, `list-observability-instances`, `destroy-observability-instance`; `prep-server` and `provision-dev-relay` gain the collector steps.
- `.minds/template/observability.sh`: the Vault schema (instance secrets, SSH keypair, origin TLS material, and the three `INGEST_CREDENTIAL_*` leaves written back by provisioning).
- `libs/mngr_imbue_cloud` (`admin server prep --extra-prep-script`) and `apps/minds` (`minds server prep` forwarding): a generic, observability-agnostic hook that appends an idempotent root script to the box prep, which is how the rendered collector install rides the pinned-host-key SSH session.
- The connector health sweep: the `/healthz` probe (small follow-up PR, after Bugsink lands).

## Testing

- Unit tests for the pure parts: config template rendering (openobserve env, caddy routes, collector configs), sender-credential and retention idempotence decisions, with the HTTP and SSH boundaries behind small interfaces per the house test style.
- The provisioning flow itself is exercised operationally against the dev instance (prototype, then steady state); no CI test provisions OVH instances.
- Deployment-test follow-up (separate PR): a `minds_services`-style test that pushes one OTLP log through the shared dev instance's public ingest path and asserts it is queryable via the API, proving the Cloudflare + caddy + auth + storage pipeline after any change.

## Implementation plan

1. **Prototype** on the dev tier per the validation plan above; feed corrections back into this spec.
2. **Provisioning**: Vault template, config templates, provisioning script + `just` recipes, DNS/Cloudflare setup, instance bring-up for dev, then staging, then production.
3. **Fleet collectors**: prep/provision-step additions; roll to existing boxes and relays via re-prep.
4. **Modal workspaces**: configure the integration for `minds-dev`, staging, production (manual, documented step).
5. **Health probe**: connector health-sweep addition once the Bugsink PR has landed.
6. **Follow-ups** (separate PRs): dashboards and alert-on-no-data rules; the deployment test; the Bugsink migration ([mngr-internal#464](https://github.com/imbue-ai/mngr-internal/issues/464)).

## Future work

- Migrate Bugsink onto this hosting pattern once it is proven: [mngr-internal#464](https://github.com/imbue-ai/mngr-internal/issues/464).
- Capacity/business metrics (pool slots, leases, active shares, LLM spend rate) emitted as OTLP metrics by an existing connector cron.
- Blackbox uptime probes of the public tier endpoints, pushed as metrics (the "inspector" pattern the Bugsink spec anticipated).
- apt_mirror Cloudflare Worker logs via Logpush to the tier R2 bucket.
- Per-request LLM traces from the LiteLLM proxy via its native OpenTelemetry callback (analogous to its `failure_callback=["sentry"]` wiring), if ever wanted; the trace ingest route already exists.
- Workspace and desktop-client telemetry, only ever with explicit user consent plumbing (separate project).
