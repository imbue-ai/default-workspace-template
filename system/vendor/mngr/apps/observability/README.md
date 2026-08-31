# observability

Per-tier observability infrastructure for the minds fleet: log and telemetry
aggregation built on
[OpenObserve](https://github.com/openobserve/openobserve) (see
`specs/minds-openobserve-telemetry.md`), and error tracking built on
[Bugsink](https://www.bugsink.com/) (see
`specs/minds-bugsink-error-tracking.md`). Both run one self-hosted instance
per tier (`production`, `staging`, and one shared `dev` instance for all
dev/CI envs) on separate small OVH Public Cloud VPSes with the same
split-plane hosting pattern.

For OpenObserve, parquet stream data lives in the tier's Cloudflare R2
bucket and metadata in the tier's Neon Postgres; for Bugsink, every acked
event is in the tier's Neon Postgres. Either way the host itself is
disposable: replacement (the upgrade path) is provision-new, stop-old,
repoint DNS.

## Ingress: split-plane

- **Machine ingest (public, narrow)**: one Cloudflare-proxied hostname per
  tier and system (`telemetry.<tier domain>` for OpenObserve,
  `errors.<tier domain>` for Bugsink). On the host, caddy terminates TLS with
  the tier's Cloudflare origin certificate and exposes ONLY the machine
  ingest routes (the OTLP routes plus `GET /healthz` on OpenObserve; the
  Sentry-protocol DSN routes on Bugsink); the origin firewall admits 443
  from Cloudflare's published ranges only. OpenObserve senders present a
  per-sender-class credential (Modal / boxes / relays), rotated
  independently; Bugsink senders authenticate with their project DSNs.
- **Human access (SSH only)**: the UI and query/REST API bind loopback.
  Operators run `ssh -L 5080:127.0.0.1:5080 debian@<instance>` (Bugsink:
  port 8300) and browse `http://localhost:<port>`. Nothing human-facing is
  ever public; there is no Tailscale and no Cloudflare Access.

## Senders

- **Modal apps**: the workspace-level OpenTelemetry integration (configured by
  hand in each Modal workspace's settings) pushes function logs, container
  metrics, and audit logs -- zero app-code changes. Auth rides in the
  workspace secret as `OTEL_HEADER_Authorization`, and
  `OTEL_HEADER_stream-name: modal_logs` names the log stream.
- **Bare-metal boxes + share relays**: a pinned `otelcol-contrib` systemd
  service (hostmetrics + journald, memory-capped, file-backed retry queue).
  Boxes get it through the prep flow (`minds-admin server prep` / `setup`
  render and install it in-process from the tier's Vault boxes credential,
  then verify the unit is active); relays via `observability
  install-collector`. Never inside the lima VMs -- per-slice visibility comes
  from box-level qemu process metrics.
- **The instance host itself**: a local collector pushing to loopback.

## Operator CLI

The `observability` CLI is the source of truth for everything on-host; the
justfile recipes (`just provision-observability`,
`just provision-observability-accounts`, `just list-observability-instances`,
`just destroy-observability-instance`) are thin wrappers that resolve the
tier's Vault entry first via `scripts/provision_observability_config.py`:

```bash
uv run observability provision --tier dev --ovh-region US-EAST-VA-1 --ssh-public-key-file key.pub
uv run observability deploy --host <ip> --tier dev --telemetry-hostname telemetry.minds-dev.com
uv run observability dns --hostname telemetry.minds-dev.com --ip <ip>
uv run observability provision-accounts --ssh-host <ip>
uv run observability render-collector-install --role box --tier dev --ingest-url https://telemetry.minds-dev.com \
    --credential-env-var OBSERVABILITY_INGEST_CREDENTIAL --out /tmp/install.sh
```

Secrets always arrive via environment variables or files (never argv); the
Vault schema lives at `.minds/template/observability.sh` ->
`secrets/minds/<tier>/observability`.

## Dashboards

Committed dashboard definitions (`imbue/observability/dashboards/*.dashboard.json`)
are the source of truth for the instances' OpenObserve dashboards; the copy on
an instance is disposable. Import them with:

```bash
uv run observability import-dashboards --ssh-host <ip>
```

The import is replace-by-title (an existing dashboard with a committed
definition's title is deleted and recreated), so re-running converges on
exactly what the repo holds. To change a dashboard, iterate on it in the UI
(SSH tunnel, like all human access), export the JSON, commit it back into
`imbue/observability/dashboards/`, and re-import on each tier. The
`fleet-version-mix` dashboard charts active clients per `X-Imbue-Client`
version, lease demand/outcomes, and the connector's `pool_gauge_sweep`
pool-composition and slot-capacity gauges.

## Bugsink (error tracking)

The `observability bugsink` command group drives the Bugsink instances the
same way; the justfile recipes (`just provision-bugsink`,
`just provision-bugsink-projects`, `just list-bugsink-instances`,
`just destroy-bugsink-instance`) resolve the tier's Vault entries first via
`scripts/provision_bugsink_config.py`:

```bash
uv run observability bugsink provision --tier dev --ovh-region US-WEST-OR-1 --ssh-public-key-file key.pub
uv run observability bugsink deploy --host <ip> --tier dev --errors-hostname errors.minds-dev.com
uv run observability bugsink provision-projects --ssh-host <ip> --tier dev
uv run observability dns --hostname errors.minds-dev.com --ip <ip>
```

The instance is a single-writer Django monolith: one gunicorn worker,
digestion inline in the ingest request (eager mode, no snappea foreman),
and every acked event durable in the tier's Neon Postgres. The venv
installs from the committed hash-locked
`deploy_assets/bugsink_requirements.txt` with `--require-hashes`; the
vendored `deploy_assets/bugsink_conf.py` settings module is re-diffed
against upstream's `docker.py.template` on every version bump.
`provision-projects` mints a REST API token by running
`bugsink-manage create_auth_token` over SSH and drives the loopback-only
canonical REST API through an SSH tunnel; the resulting per-service project
DSNs land in the tier's `sentry` Vault entry (written by the glue script),
which `minds-admin env deploy` stamps into the reporting services' Modal secret.
Vault schema: `.minds/template/bugsink.sh` -> `secrets/minds/<tier>/bugsink`.
The operator runbook is `apps/minds/docs/deploy/bugsink-bringup.md`.

## Retention

Metrics keep 25 months (the instance-wide default -- OpenObserve maps each
OTLP metric to its own stream); the known log streams are overridden down to
90 days at provisioning time (log lines can carry user-identifying data).
Bugsink events keep 30 days instance-wide (`MAX_EVENT_AGE_DAYS`, enforced at
digest time; the compensating control for prompt-bearing LiteLLM failure
payloads -- issue rows survive event deletion).

## Status

Implements `specs/minds-openobserve-telemetry.md`; the spec's prototype
validation plan is the first operational step and may adjust the pinned API
shapes (sender user role, stream-settings endpoint, Modal's endpoint URL
shape).
