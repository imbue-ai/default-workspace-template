# share_relay

Self-hosted sharing relays for the minds workspace-sharing redesign.

A relay is a small OVH Public Cloud instance running [frp](https://github.com/fatedier/frp)'s
`frps` in **SNI-passthrough** mode. It reads the ClientHello SNI of each inbound
TLS connection on port 443 and splices the raw byte stream into the matching
workspace tunnel. It never terminates TLS, so it sees only ciphertext and holds
no TLS certificates and no per-share credentials: TLS terminates *inside* the
workspace container. Its only secret is the connector plugin-auth URL embedded
in `frps.toml` (installed root-only), which authorizes tunnel operations.
It also holds no per-share state -- every tunnel `Login` / `NewProxy` operation
is authorized by an HTTP callback to the connector, so a workspace's `frpc` can
only claim the hostnames its relay token is allowed to.

## Hostnames and regions

Workspace hostnames are `<service>.<host-id>.<user-id>.<region>.imbueminds.com`.
The `<region>` label (`us1` = OVH Hillsboro, `us2` = OVH Vint Hill) is the label
directly under the content apex, so:

- One static wildcard DNS record per region (`*.us1.imbueminds.com` -> that
  relay's IP) covers every workspace and service at any depth -- no per-share
  DNS.
- `<region>.imbueminds.com` is the Public-Suffix-List entry that makes each
  `<user-id>.<region>.imbueminds.com` its own registrable site, isolating one
  user's workspaces from another's while keeping a single user's services
  same-site.

Region codes are config, not code: a new region (or a per-developer dev relay
like `dev-josh-1`) is a `RegionCode` plus a wildcard DNS record. Only `us1` /
`us2` exist today; the expansion scheme (us3+, eu/sa/ap/au/me/af) is reserved.

## What this package is

The operator CLI (`share-relay`) is the source of truth for a relay's on-disk
config, so the deploy step stays a dumb copy and the config is unit-testable:

```bash
# Render a region's config artifacts into a directory.
share-relay render --region us1 --content-domain imbueminds.com \
    --plugin-auth-url https://<connector>/frps/auth --out-dir ./out
# -> out/frps.toml, out/nftables.conf, out/port80.Caddyfile

# Serve the liveness endpoint on a relay host (systemd unit; GET /healthz).
share-relay healthcheck
```

The same CLI drives the relay's operational lifecycle (the justfile recipes are
thin wrappers over these): `provision` creates the instance on OVH Public
Cloud, `deploy` renders the config and installs it -- plus the pinned frps and
the healthcheck script -- over SSH, restarting the services, `dns` upserts the
region's records, and `list` / `destroy` manage existing instances.

- `frps.toml` -- SNI-passthrough vhost + the connector-auth server plugin
  (`Login` / `NewProxy` only; visitor connections never call the connector).
- `nftables.conf` -- tier-2 abuse guard: per-source-IP new-connection rate and
  concurrent-connection caps on the vhost port. (Per-workspace bandwidth quotas
  are tier 3, deferred, and enforced connector-side.)
- `port80.Caddyfile` -- a dumb `:80 -> https` redirector so bare `http://` links
  don't hang (SNI passthrough is 443-only).

`imbue/share_relay/deploy_assets/cloud-init.yaml` provisions a fresh instance (nftables, caddy, the frps
+ healthcheck systemd units); everything version- or config-shaped is applied
afterwards by `share-relay deploy` over SSH, so changes never need a reimage.

## Status

Experimental. Part of the self-hosted sharing redesign
(`blueprint/sharing-redesign/`).
