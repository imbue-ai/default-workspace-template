# share-gateway

The in-workspace sharing stack for the self-hosted relay design: when this
workspace is shared, TLS terminates *here* (never at the relay), every request
is authorized against the owner's grants file, and an outbound frp tunnel
carries the encrypted bytes to the region's relay.

## How it works

The supervisord program `share-gateway` runs `share_gateway.runner`, which
watches `data/.secrets/share.env` (written by the Imbue Studio desktop app at
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
   workspace-domain session cookie (30 days). What a failed callback shows is
   described under "Login outcomes".
2. **caddy** (`127.0.0.1:8443`): terminates the share's real TLS with the
   cert/key under `data/.secrets/share_tls/` and routes by Host -- the bare
   workspace domain to `system_interface`, `<service>.<domain>` to that
   registered service's local backend (from `data/.state/apps.toml`; the
   Caddyfile re-renders and hot-reloads when the registry changes), and
   unknown-but-plausible service origins to an auto-retrying loading page.
   Every response it sends, a service's or its own, carries the browser
   hardening headers (`Strict-Transport-Security`, `X-Content-Type-Options:
   nosniff`, `Referrer-Policy: same-origin`, and a `frame-ancestors` policy
   naming who may embed the workspace) and no `Server` header, so a service
   gets them without setting any itself; a service's own
   `Content-Security-Policy` is left in place beside the gateway's.
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
the chain. Key and cert persist across unshare for a fast re-share; a daily
check renews the cert when it is within 30 days of expiry. The cookie signing
secret does not persist: unsharing deletes `data/.secrets/share_gateway_signing_key`,
so every session -- the owner's included -- stops verifying the moment the
share ends, and the next share mints a fresh secret. The secret file also
records the SHA-256 of the relay token it was minted under, and the runner
reuses a stored secret only under that same token. Every share carries a new
relay token, so a secret an unshare left behind while the runner was down
never signs the next share: it is discarded when the runner next starts
without share materials, and replaced at stack start when the runner comes
back to find the next share's materials already in place.

## The session

The session cookie carries the visitor's identity record -- `user_id` and
`email` (verified) -- plus the `owner` flag, copied from the broker's handoff
token at login, and lasts 30 days. Nothing else is in it: profile data
(display name, profile picture) lives in the connector and is fetched on
demand by whatever renders it, so the gateway plays no part in profiles. A
cookie minted before the record carried a user id opens no session: an HTML
navigation is silently bounced through the broker again, and a fetch answers
401 until the tab next navigates. Profile claims a cookie from an older gateway carries are
ignored.

## Login outcomes

The callback tells a visitor what actually happened instead of one generic
refusal, and logs every denial (never the token) to the service's stderr:

- A callback whose nonce is unknown, already used, or expired, or whose token
  does not verify (expired, replayed, minted for another nonce) is usually a
  reopened link or a slow redirect, so it heals itself. A visitor who already
  holds a valid session is sent straight on to their `next` (validated as one
  of this workspace's origins; anything else lands on the shell). Anyone else
  is sent through the broker exactly once more, on a nonce the gateway records
  as the retry, with the "Continue as" step already confirmed. If that retry
  fails too, the visitor sees **"Sign-in link expired"** (403: "This sign-in
  link was already used or has expired. Open the workspace link again.") --
  never a redirect loop. Nonces stay single-use throughout.
- A missing or malformed grants file is **"Sharing is misconfigured"** (503),
  telling the visitor the owner must fix the workspace's sharing settings.
  `/_auth/verify` answers the same way for a non-owner mid-session; the owner
  is never grant-checked, so they still get in.
- A verified account with no grant is **"Not shared with you"** (403). So is
  one whose only grants are per-service grants on internal apps (see
  "Grants") or on apps not registered, since those admit it nowhere.

## When the stack cannot come up

Bringing the stack up needs the connector twice (the certificate, then the
relay assignment). A failed attempt is not retried on the next 10-second tick:
the runner waits 15s, 30s, 1m, 2m, 8m, then 15m between attempts (a longer
connector `Retry-After` wins), and a refusal the connector marks as permanent
(any 4xx other than 408/429, e.g. an invalid CSR) halts retries until
`share.env` changes -- a re-share from the desktop starts over. Each outcome is
written to `data/.state/share_gateway/status.json`:

```json
{"state": "retrying", "workspace_domain": "...", "failed_attempt_count": 2,
 "last_error": "certificate provisioning failed: ...",
 "next_retry_at": "2026-09-13T12:01:00+00:00", "updated_at": "..."}
```

`state` is `up`, `retrying`, or `halted`. The Imbue Studio desktop client reads this
file to explain a share that is not live yet; it is removed at unshare.

The runner also records the share's domain (`SHARE_WORKSPACE_DOMAIN`) in
`data/.state/share_domain` whenever it reads new materials, and never removes it:
the shell builds each app's share address from it (`share_url` in its inventory,
the `link` of `workspace-layout list`), and an address an agent wrote under it
should keep opening the app's window after an unshare. A re-share keeps the
domain.

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
that service's origin (the shell and siblings stay 403), and only for an app
the registry does not mark `internal`. An internal app (the shell itself, the
terminal's pty, the owner and VM exec services) is reached only through a
workspace-level grant: a per-service grant on it admits nobody, and a visitor
whose only grants are on such apps is refused at login. The gateway reads this
from `data/.state/apps.toml` on every request, beside the label map. Within a
scope the visitor's `user_id` is matched against `users` first, then their verified
email against `emails` (case-insensitive), then its domain against
`email_domains`. A document written before `users` existed reads as having
none.

A `users` entry is an account's user id, the durable identity. An `emails`
entry is an invitation: once a visitor with that verified email is admitted
(at the login callback or on any later request), the gateway rewrites the
document to hold their user id instead -- the email leaves every scope's
`emails` and the user id joins that scope's `users` -- so a later email change
on their account never revokes what the owner granted. The Imbue Studio desktop's
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
arranges; the origin's own pages (a chat at `/<agent-id>`, the Files app's
listing) are what they see, and a `[services.files]` grant admits only the
Files app. Whether an app declares `system` (part of the workspace itself,
which the Imbue Studio Share tab lists under its System group) makes no
difference here. Nothing here is configured per app: caddy re-renders its
routes from the registry, so the chat origin (like every app's) is claimed and
routed as soon as the app registers.

## Request identity (what a service sees)

Every request that reaches a backend carries one gateway-set header, and a
service can trust it because caddy strips any inbound copy before
`forward_auth` and re-injects only the verified value from `/_auth/verify`.
(The strip is a `request_header` directive in the same `handle` block as
`forward_auth`; caddy's built-in directive order would run it *after*
`forward_auth` and delete the injected value, so the rendered Caddyfile's
global options carry `order request_header before forward_auth`.)

```
X-Imbue-Identity: {"owner":true,"user_id":"…","email":"…"}
X-Imbue-Identity: {"owner":false,"user_id":"…","email":"…"}
```

- `owner` is always present.
- `user_id` and `email` are always present over a share (a session only
  exists for a signed-in account, and a visitor's email is always verified).
- Nothing else. Profile data (display name, profile picture) is not in the
  header: it lives in the connector, and a consumer that renders it looks it up
  by `user_id`.

This is the same header the local `mngr forward` path stamps, so a service
codes against it identically whether reached over the relay or locally. On
the local forward the single authenticated user is the owner: for a shared
workspace the desktop hands the forward the owner's record and the header
carries it; for an unshared workspace it carries `{"owner":true}` and nothing
else, so a service must cope with the owner's identity being unknown. A
request with no header at all came through no current proxy; a service treats
it as `{"owner":true}`, never as a visitor.

Services key behavior on `user_id`; `email` is for display beside whatever
profile they fetch, never a substitute identity.

## Visiting agents

A coding agent running outside the workspace (on the user's laptop, in a cloud
agent product, on a collaborator's machine) can be invited in the way a person
is, and then works through the workspace with exactly the workspace's authority
(`specs/studio-agent-access/spec.md` in the mngr repo). Agents are a grant kind
of their own, kept apart from the human grants in
`data/.secrets/agent_access/grants.toml` (one `[[agents]]` table per grant,
written only by this service and its `agent-access` CLI, so a desktop that
rewrites `share_grants.toml` wholesale can never touch them) and admitted by
the same gateway:

- **The invite.** `POST /_agent/invites` (a session cookie of the owner or of a
  visitor with a workspace-level grant, or the bearer token of a workspace-scope
  agent) mints a one-time code: twelve Crockford base32 characters, fifteen
  minutes, consumed by one join, burned after five failed attempts. It answers
  the code and the instruction block the inviter copies, rendered from
  `.agents/shared/references/visiting-agents-invite.md`. The scope is chosen at
  minting: `workspace` (every origin, exec authority) or a list of grantable
  app names (those origins only, no exec). The desktop mints through
  `agent-access invite` over `mngr exec`, which also works while the workspace
  is unpublished.
- **The join.** `POST /_agent/join` on the auth origin, unauthenticated and rate
  limited per client address, takes `{code, public_key, name}`: the agent's own
  Ed25519 key (generated by the helper and never leaving the agent's machine)
  and the name it chose (held to the granter-written-name rule). It writes the
  grant, appends a marked `authorized_keys` line for workspace scope, and
  answers the admission token (shown once, stored hashed), the exec origin,
  the owner-exec audience (`container:<host-id>`), the container's host public
  key, and the app origins the scope admits. A key that already holds a grant
  re-joins: same agent id, a fresh token, the new invite's scope and granter.
- **Admission.** `/_auth/verify` accepts `Authorization: Bearer <token>` beside
  the session cookie. The token names a grant; the grant's scope decides which
  origins it may reach (workspace scope reaches internal apps too, an app scope
  only its apps while they are registered and not internal); the Origin policy
  is skipped, since a bearer header is never ambient; and the identity handed on
  is `{"owner":false,"agent_id":...,"agent_name":...,"granter":...}` with no
  `user_id`. Caddy strips the `Authorization` header before the backend sees
  it, as it strips the session cookie. owner-exec verifies the request
  signature itself; the gateway never does, so the two cannot drift.
- **The key lines are reconciled from the grants file** at every start and on
  every join and revoke (`agent_grants.reconcile_authorized_keys`): one line
  per unexpired workspace-scope grant, carrying the `imbue-agent-access
  agent_id=...` marker; every other line of the file is left alone. The
  read-and-replace holds `~/.ssh/authorized_keys.lock`, the lock every other
  writer of the file (the desktop's cross-workspace SSH grant) takes around its
  own read-and-write, so neither can overwrite the other's lines.
- **The CLI's answers.** `agent-access <command> --json` prints one JSON
  document on stdout; a refusal or a misconfiguration is one JSON object on
  stderr, `{"error": ..., "kind": "refused" | "misconfigured"}`, with exit code
  2 or 3. `--granter`, `--revoker`, and `--owner` are what the caller asserts:
  the CLI trusts whoever reached the workspace with exec, and each event it
  records says the CLI asserted it, as opposed to the gateway, which verifies
  its caller.
- **Orientation.** `GET /_agent/primer` renders
  `.agents/shared/references/visiting-agents.md` with the skills index, the
  apps (each with the one-line purpose and the API-description address its
  manifest declares, `description` and `api_schema_path`), the connected accounts and granted permissions (read through latchkey,
  cached a minute), and the recent changes filled in, cut to the agent's scope;
  `GET /_agent/skills` is the index (name, description, path, a content hash
  per skill and when it last changed, computed once per HEAD commit); `GET
  /_agent/changes?since=` the git log since a time, the `update-self:` merges
  among it, and the skills touched. `GET /_agent/me` repeats the join answer
  minus the token, so a helper can heal moved coordinates.
- **The helper.** `helper/wsvisit.cjs` is a single Node 18+ script with no
  dependencies. The instruction block installs it from this repository on
  GitHub at the workspace's template commit (`agent_orientation.template_commit`:
  the commit the workspace was created from or last updated to, never its own
  HEAD, which bootstrap's initial commit puts past anything published; its tag when
  it has one)
  with its SHA-256 to check; `GET /_agent/helper` serves the same file (with
  `X-Helper-Version` and `X-Helper-SHA256` headers) as the fallback for an
  unpublished commit, and the join and `me` answers carry a `helper` object
  the script compares its own hash against. It joins, signs owner-exec requests
  per the profile (its tests run against the owner-exec vectors vendored
  beside it), verifies responses and stream trailers against the pinned host
  key, reaches an app over the share with `get <app>/<path>` (the admission
  token as a bearer, sent to the workspace's origins only, waiting out an app
  that answers 503 with `Retry-After` while it starts), refreshes the skills
  index when its copy is a day old and prints what
  changed, and re-reads `me` and retries once on a 403 or a verification
  failure, except for a `run` whose output already streamed: that is reported
  rather than run again, and the refreshed addresses serve the next command.
- **Grants, revocation, audit.** `GET /_agent/grants` lists what the caller may
  see (the owner everything, anyone else what they granted);
  `DELETE /_agent/grants/<id>` revokes (the owner any, a granter their own).
  Invites, joins, revocations, and expiries land in
  `$MNGR_AGENT_STATE_DIR/events/agent_access/events.jsonl`, the owner's record
  of the grants' lifecycle. Nothing records what an agent ran: a workspace-scope
  agent holds the workspace's authority and can edit any file the workspace
  keeps, so such a log would prove nothing.

Grants, keys, and tokens survive an unpublish; only the tunnel goes away.
