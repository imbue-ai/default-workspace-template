New `share_relay` project: the self-hosted SNI-passthrough relays for workspace sharing.

A relay is a small OVH Public Cloud instance running frp's `frps` in SNI-passthrough mode (routes inbound TLS by ClientHello SNI, never terminates it). The operator CLI (`share-relay`) renders a region's on-disk config -- `frps.toml` (SNI vhost + a connector-auth server plugin scoped to `Login`/`NewProxy` only), an `nftables.conf` tier-2 abuse guard (per-source-IP connection rate + concurrency caps on the vhost port), and a dumb `:80 -> https` redirector -- and serves a `/healthz` liveness endpoint. A `deploy/cloud-init.yaml` provisions the instance and its systemd units.

Workspace hostnames are `<service>.<host-id>.<user-id>.<region>.imbueminds.com`; each region is one wildcard DNS record and one Public-Suffix-List entry. `us1`/`us2` are defined; further regions are config only.
