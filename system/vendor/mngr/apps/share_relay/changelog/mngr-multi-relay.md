Multi-relay sharing, phase 1 (blueprint/multi-relay): regions run several relays.

Relays get an opaque `relay-<hex>` id rendered into the frps plugin-auth path (so the connector can attribute Login/NewProxy callbacks per relay), instance names gain an ordinal (`share-relay-<env>-<region>-<n>`), and new `register` / `deregister` CLI commands write the connector's relay fleet inventory as the final provisioning step.

The `dns` command now reconciles the region's A-record SET (pass `--ip` per relay; TTL 60) instead of converging on a single IP; the connector's health sweep maintains the same records in steady state.

New manual verification harness (`uv run python -m imbue.share_relay.frp_verification`) pinning the frp behaviors the design rests on -- unknown-SNI fast-fail alert, no inbound PROXY protocol on the vhost, independent same-domain claims on two servers with preserved visitor identity -- to re-run on every frp version bump.
