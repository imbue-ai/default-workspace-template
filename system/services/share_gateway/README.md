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
   `forward_auth` backend. `/_auth/verify` checks the session cookie (set as
   two copies of one signed value: `imbue_machine_session`, `SameSite=Lax`,
   for top-level visits, and `imbue_machine_session_partitioned`,
   `SameSite=None` with the CHIPS `Partitioned` attribute, for the hosted
   chrome's cross-site iframe -- either copy opens the session), re-reads
   `data/.secrets/share_grants.toml` on every request
   (revocation is instant; a malformed file fails closed), enforces the Origin
   policy (WebSocket upgrades need a workspace Origin; non-GETs reject a
   foreign one), strips the session cookie from what the service sees, and
   hands the service the verified identity (see "Request identity"). Visitors
   without a session are redirected to the accounts broker and come back to
   `/_auth/callback`, which verifies the broker's 60-second RS256 handoff
   token (JWKS, audience, nonce, single-use jti) and sets the
   workspace-domain session cookie (24h). `/_auth/refresh` re-runs that
   handoff on demand (see "Refreshing your identity").
2. **caddy** (`127.0.0.1:8443`): terminates the share's real TLS with the
   cert/key under `data/.secrets/share_tls/` and routes by Host -- the bare
   workspace domain to `system_interface`, `<service>.<domain>` to that
   registered service's local backend (from `data/.state/apps.toml`; the
   Caddyfile re-renders and hot-reloads when the registry changes), and
   unknown-but-plausible service origins to an auto-retrying loading page.
3. **frpc, one per relay**: outbound tunnels to EVERY relay of the region
   (the multi-relay design: any relay can then serve any visitor),
   authenticated by the per-share relay token (in the client metadata; the
   connector authorizes every Login/NewProxy). Each claims exactly this
   workspace's registered service labels + the auth label. The relay set comes
   from the connector's `GET /shares/assignment` (relay-token auth), fetched
   at stack start, re-polled on the server-provided interval, and cached under
   `data/.state/share_gateway/assignment.json` so restarts work with the
   connector down; `share.env` carries no relay endpoint.

The TLS private key is generated in the workspace and never leaves it: the
runner sends a CSR to the connector, which completes ACME DNS-01 and returns
the chain. Key, cert, and the cookie signing secret persist across unshare for
a fast re-share; a daily check renews the cert when it is within 30 days of
expiry.

## The session

The session cookie carries the visitor's whole identity record -- `user_id`,
`email` (verified), `display_name`, `avatar_url` -- plus the `owner` flag,
copied from the broker's handoff token at login. It is the only per-request
source of identity the gateway has: nothing polls the connector for profile
data. A cookie minted before the record carried a user id opens no session:
an HTML navigation is silently bounced through the broker again, and a fetch
answers 401 until the tab next navigates.

## Grants

`data/.secrets/share_grants.toml`:

```toml
[workspace]
users = ["3f1c..."]
emails = ["friend@example.com"]
email_domains = ["partner.org"]

[services.web]
users = []
emails = ["reviewer@example.com"]
email_domains = []

[services.chat]
users = []
emails = ["pair@example.com"]
email_domains = []
```

Workspace-level grants admit every service; per-service grants admit exactly
that service's origin (the shell and siblings stay 403). Within a scope the
visitor's `user_id` is matched against `users` first, then their verified
email against `emails` (case-insensitive), then its domain against
`email_domains`. A document written before `users` existed reads as having
none.

A `users` entry is an account's user id, the durable identity. An `emails`
entry is an invitation: once a visitor with that verified email is admitted
(at the login callback or on any later request), the gateway rewrites the
document to hold their user id instead -- the email leaves every scope's
`emails` and the user id joins that scope's `users` -- so a later email change
on their account never revokes what the owner granted. The minds desktop's
grants editor writes `users` entries directly when it can resolve an address
to an account.

Every writer of the grants file holds an exclusive `flock` on the sibling
`data/.secrets/share_grants.toml.lock` around its read-modify-write and
replaces the file atomically (a same-directory temp file, then a rename): the
gateway's upgrade and the desktop's `mngr exec` writes alike. A whole-document
save the desktop built before an upgrade can still land afterwards and put the
email back without the user id; the visitor keeps access through the email,
and the next visit upgrades it again.

The `[services.<name>]` key is the app's registered name (the `name` in its
`app.toml`). The chat app is one of them: its pages are served at
their own registered origin (`chat-<rand>.<domain>`), framed by the shell, so a
workspace-level grant admits the chat origin directly and a `[services.chat]`
grant narrows a visitor to it. A visitor holding only a per-app grant reaches
that app's origin and nothing else -- not the shell, so not the tabs the shell
arranges; the origin's own pages (a chat at `/<agent-id>`, the file viewer's
listing) are what they see, and a `[services.files]` grant admits only the
file viewer. Nothing here is configured per app: caddy re-renders its routes
from the registry, so the chat origin (like every app's) is claimed and routed
as soon as the app registers.

## Request identity (what a service sees)

Every request that reaches a backend carries one gateway-set header, and a
service can trust it because caddy strips any inbound copy before
`forward_auth` and re-injects only the verified value from `/_auth/verify`:

```
X-Imbue-Identity: {"owner":true,"user_id":"…","email":"…","display_name":"…","avatar_url":"…"}
X-Imbue-Identity: {"owner":false,"user_id":"…","email":"…","display_name":"…","avatar_url":"…"}
```

- `owner` is always present.
- `user_id` and `email` are always present over a share (a session only
  exists for a signed-in account, and a visitor's email is always verified).
- `display_name` and `avatar_url` are present when the account has them and
  omitted (not null) otherwise.

This is the same header the local `mngr forward` path stamps, so a service
codes against it identically whether reached over the relay or locally. On
the local forward the single authenticated user is the owner: for a shared
workspace the desktop hands the forward the owner's record and the header
carries it; for an unshared workspace it carries `{"owner":true}` and nothing
else, so a service must cope with the owner's identity being unknown. A
request with no header at all came through no current proxy; a service treats
it as `{"owner":true}`, never as a visitor.

Services key behavior on `user_id`, render `display_name` with `email` beside
it, and never treat a display name as an identity: names are self-asserted
and mutable.

## Refreshing your identity

The record in a session is as fresh as its handoff. A user who changed their
name or avatar makes their own requests carry it before the session expires
by navigating to `/_auth/refresh?next=<url>` -- served at every origin of the
share (the shell links to it on its own origin) -- which mints a pending
login and sends the browser through the broker exactly as a first visit does,
with the "Continue as ..." step already confirmed. The callback re-sets the
cookie from the fresh token and lands on `next` (which must be one of this
workspace's own origins; anything else falls back to the shell).
