# share-gateway

The in-workspace sharing stack for the self-hosted relay design: when this
workspace is shared, TLS terminates *here* (never at the relay), every request
is authorized against the owner's grants file, and an outbound frp tunnel
carries the encrypted bytes to the region's relay.

## How it works

The supervisord program `share-gateway` runs `share_gateway.runner`, which
watches `data/.secrets/share.env` (written by the minds desktop app at
share-enable, removed at unshare). While the materials are present it keeps
three things running:

1. **The gateway HTTP service** (Flask, `127.0.0.1:8791`): caddy's
   `forward_auth` backend. `/_auth/verify` checks the `imbue_machine_session`
   cookie, re-reads `data/.secrets/share_grants.toml` on every request
   (revocation is instant; a malformed file fails closed), enforces the Origin
   policy (WebSocket upgrades need a workspace Origin; non-GETs reject a
   foreign one), and strips the session cookie from what the service sees.
   Visitors without a session are redirected to the accounts broker and come
   back to `/_auth/callback`, which verifies the broker's 60-second RS256
   handoff token (JWKS, audience, nonce, single-use jti) and sets the
   workspace-domain session cookie (24h).
2. **caddy** (`127.0.0.1:8443`): terminates the share's real TLS with the
   cert/key under `data/.secrets/share_tls/` and routes by Host -- the bare
   workspace domain to `system_interface`, `<service>.<domain>` to that
   registered service's local backend (from `data/.state/apps.toml`; the
   Caddyfile re-renders and hot-reloads when the registry changes), and
   unknown-but-plausible service origins to an auto-retrying loading page.
3. **frpc**: the outbound tunnel to the region's relay, authenticated by the
   per-share relay token (in the client metadata; the connector authorizes
   every Login/NewProxy). It claims exactly this workspace's domain + wildcard.

The TLS private key is generated in the workspace and never leaves it: the
runner sends a CSR to the connector, which completes ACME DNS-01 and returns
the chain. Key, cert, and the cookie signing secret persist across unshare for
a fast re-share; a daily check renews the cert when it is within 30 days of
expiry.

## Grants

`data/.secrets/share_grants.toml`:

```toml
[workspace]
emails = ["friend@example.com"]
email_domains = ["partner.org"]

[services.web]
emails = ["reviewer@example.com"]
email_domains = []
```

Workspace-level grants admit every service; per-service grants admit exactly
that service's origin (the shell and siblings stay 403). Matching is
case-insensitive.

## Request identity (what a service sees)

Every request that reaches a backend carries two gateway-set headers, and a
service can trust them because caddy strips any inbound copy before
`forward_auth` and re-injects only the verified values from `/_auth/verify`:

- **`X-Share-Owner`** -- always present, `true` or `false`.
- **`X-Share-Email`** -- present **only when `X-Share-Owner: false`**: the
  verified email of the non-owner visitor making the request. It is absent for
  owner requests; the owner's email is deliberately never revealed per-request.

This is the same contract the local `mngr forward` path honors, so a service
codes against it identically whether reached over the relay or locally. Locally
the single authenticated user is always the owner, so `X-Share-Owner: true`
and no `X-Share-Email` -- a service that needs per-visitor behavior keys off
`X-Share-Email` whenever `X-Share-Owner` is `false`.

## Owner email (only while shared)

Because the owner's email never rides a request header, a service that needs it
reads it from a dedicated file the minds app writes when the workspace is
shared and removes when it is unshared:

```
data/.state/share/owner_email
```

The file holds the owner's account email (no trailing newline) and exists
**only while sharing is active**, so its mere presence is a reliable "this
workspace is shared" signal. It is absent for an unshared (e.g. purely local
Docker) workspace, and may also be absent if the owner never signed into an
Imbue account -- a service must tolerate it being missing.
