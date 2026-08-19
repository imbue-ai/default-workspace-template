Added `specs/minds-openobserve-telemetry.md`: the design for aggregating logs and server telemetry (OTEL) from the minds infrastructure fleet into one self-hosted OpenObserve instance per tier, hosted on a small OVH VPS with R2-backed data and Neon-backed metadata.

Modal-side telemetry (function logs, container metrics, audit logs) arrives via Modal's workspace-level OpenTelemetry integration with zero app-code changes; bare-metal boxes and share relays run a pinned OpenTelemetry Collector installed by the existing prep/provision flows.

Ingress is split-plane: a Cloudflare-proxied hostname per tier exposes only the OTLP ingest routes (per-sender-class tokens), while the web UI is reachable exclusively over an SSH tunnel.

Implemented the spec: the new `apps/observability` project (operator CLI), plus `scripts/provision_observability_config.py` (the Vault glue), `.minds/template/observability.sh` (the Vault schema), and the `provision-observability` / `provision-observability-accounts` / `list-observability-instances` / `destroy-observability-instance` recipes in `private.just` (the accounts recipe re-runs credential minting and log-stream retention against an existing instance, so retention re-application never provisions a duplicate VPS); `prep-server` and `provision-dev-relay` now install the pinned OpenTelemetry Collector whenever the tier's observability Vault entry is populated (clean skip otherwise).

The new `apps/observability` project (like the other deployment/infra apps), `scripts/provision_observability_config.py`, and `.minds/template/observability.sh` are deliberately absent from the public mirror: none match the `mirror/copy.bara.sky` allowlist, and the allowlist's apps comment plus the mirror spec's private-path table now name observability explicitly.

Filed the follow-up to migrate Bugsink onto the same hosting pattern once proven: https://github.com/imbue-ai/mngr-internal/issues/464.
