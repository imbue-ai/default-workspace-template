# observability

Per-tier log and telemetry aggregation for the minds infrastructure fleet,
built on [OpenObserve](https://github.com/openobserve/openobserve). See
`specs/minds-openobserve-telemetry.md` for the full design.

One self-hosted OpenObserve instance runs per tier (`production`, `staging`,
and one shared `dev` instance for all dev/CI envs) on a small OVH Public Cloud
VPS. Parquet stream data lives in the tier's Cloudflare R2 bucket and metadata
in the tier's Neon Postgres, so the host itself is disposable: replacement
(the upgrade path) is provision-new, quiesce-old, repoint DNS.

## Ingress: split-plane

- **Machine ingest (public, narrow)**: one Cloudflare-proxied hostname per
  tier (`telemetry.<tier domain>`). On the host, caddy terminates TLS with the
  tier's Cloudflare origin certificate and exposes ONLY the OTLP ingest routes
  plus `GET /healthz`; the origin firewall admits 443 from Cloudflare's
  published ranges only. Every sender presents a per-sender-class credential
  (Modal / boxes / relays), rotated independently.
- **Human access (SSH only)**: the UI and query API bind loopback. Operators
  run `ssh -L 5080:127.0.0.1:5080 debian@<instance>` and browse
  `http://localhost:5080`. Nothing human-facing is ever public; there is no
  Tailscale and no Cloudflare Access.

## Senders

- **Modal apps**: the workspace-level OpenTelemetry integration (configured by
  hand in each Modal workspace's settings) pushes function logs, container
  metrics, and audit logs -- zero app-code changes. Auth rides in the
  workspace secret as `OTEL_HEADER_Authorization`, and
  `OTEL_HEADER_stream-name: modal_logs` names the log stream.
- **Bare-metal boxes + share relays**: a pinned `otelcol-contrib` systemd
  service (hostmetrics + journald, memory-capped, file-backed retry queue).
  Boxes get it through the prep flow (`minds server prep` passes the rendered
  script via `--extra-prep-script`); relays via `observability
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

## Retention

Metrics keep 25 months (the instance-wide default -- OpenObserve maps each
OTLP metric to its own stream); the known log streams are overridden down to
90 days at provisioning time (log lines can carry user-identifying data).

## Status

Implements `specs/minds-openobserve-telemetry.md`; the spec's prototype
validation plan is the first operational step and may adjust the pinned API
shapes (sender user role, stream-settings endpoint, Modal's endpoint URL
shape).
