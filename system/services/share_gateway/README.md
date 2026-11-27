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
   `Content-Security-Policy` is left in place beside the gateway's. It also
   compresses what the services serve: a plain `GET` of a text type (HTML,
   CSS, JavaScript, JSON, plain text, SVG) goes out zstd- or gzip-encoded,
   while a `HEAD`, a ranged request, an event stream and any
   already-compressed type pass through untouched.
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

## The Screen's media path

While the stack is up the gateway also brokers the Screen's stream for visitors
(`docs/system/specs/screen/spec.md` section 15, `specs/turn-relay/spec.md` section 7):

- Every relay's frpc config carries a second proxy, `screen-media` of type `tcp`,
  `localIP` the workspace's own interface address, `localPort` 59000 (neko's TCP ICE mux
  inside the workspace) and `remotePort` the assignment's `media_port`, so a visitor's
  browser can dial any relay of the region at that port and reach the Screen. The local
  address is the interface's, never loopback: neko's ICE stack keys its TCP mux by the
  address a connection arrives on and gathers a candidate for the interface alone, so a
  connection spliced to loopback is accepted and then read by nothing
  (`local_address.py`). The proxy is rendered only when the assignment carries a
  `media_port` and the workspace has a routable address; a connector or a share without
  one renders none (an older connector's answer still parses, with `media_port` null and
  no `published_endpoints`).
- At stack start, and again at half life (the credential lives one hour), the gateway
  fetches `GET /turn/ice-servers` from the connector with the relay token and keeps the
  answer in memory only. A refusal (plan, quota, rate limit) or an unreachable connector
  leaves an empty list and the next attempt five minutes away; a credential already held
  keeps being served until it expires. The share never fails over it.
- `GET /connect-info` on the loopback port (`127.0.0.1:8791`, beside `/_auth/*`) answers
  `{"ice_servers": [...], "candidates": [...]}` to the Screen service: the ICE servers as
  fetched, and as candidates the assignment's `published_endpoints` (the box's own ports,
  for a cloud workspace) plus one `tcp` candidate per relay address at `media_port`, each
  with its `guest_port`. caddy routes a visitor to this app only for `/_auth/*`, `/_agent/*`
  and `/_health`, so the credential is never served on a visitor-facing route.

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

`state` is `up`, `retrying`, or `halted`. While the stack is up the runner
rewrites the file once a minute, so `updated_at` says whether a runner is alive
to write it: the Imbue Studio desktop client reads this file to explain a share
that is not live yet, and disbelieves an `up` stamped more than five minutes
ago (a container restarted with its services down leaves the last `up` behind
until the services come back). It is removed at unshare.

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
terminal's pty) is reached only through a workspace-level grant: a per-service
grant on it admits nobody, and a visitor whose only grants are on such apps is
refused at login. The gateway reads this from `data/.state/apps.toml` on every
request, beside the label map. Within a
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
- `is_from_web_client` is present, and `true`, only on a request the hosted
  web client's page sent to a workspace route (see "The web client's page").
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

## The web client's page

The hosted web client (`SHARE_CHROME_ORIGIN`) is a page of another origin than
the workspace's. It frames the workspace, and it also calls two things itself,
with the workspace's session cookie: the workspace routes an internal app
serves (`/api/workspace/...`, the shell's), and the gateway's own
`/_agent/...` routes. For a request that names the web client's origin and is
for one of those paths (on an internal app's label for the first, on the auth
label for the second), and for no other request:

- caddy answers the browser's permission check (`OPTIONS`) itself, with the web
  client's origin, credentials allowed, the methods `GET`, `POST`, `PUT`,
  `PATCH` and `DELETE`, and the `Content-Type` header. The check carries no
  session and never reaches the app, whose request guard would refuse it.
- caddy adds `Access-Control-Allow-Origin` (the web client's origin) and
  `Access-Control-Allow-Credentials: true` to every answer, a refusal of the
  gateway's included, so the page can read a `401` for a lapsed session.
- `/_auth/verify` checks the session as it does for every request, and marks
  the identity it stamps on a workspace route's request with
  `"is_from_web_client": true`. The app's request guard admits a request of
  another origin on that mark alone, and only on the routes the app names
  (`app_schema.request_guard`).

The mark does not ask for the owner: a grantee of the whole workspace gets it
too, as that grantee already reaches the terminal. A program that is not a
browser can send any `Origin` and gains nothing by it, since it holds no
session and the mark is set only on a request whose session the gateway
accepted. A share with no web client (`SHARE_CHROME_ORIGIN` unset) renders
none of this. Neither does one whose `SHARE_CHROME_ORIGIN` is not a plain
http(s) origin (a scheme, a host and at most a port): the value is written into
caddy's matchers, so anything else is read as no web client, the share comes up
without one, and the gateway's log says so.

`share_gateway/web_client.py` holds the paths, the mark's name and what the
permission check allows. `system/test_web_client_routes.py` holds them to what
the shell serves and what its request guard reads.

## The access log

caddy writes one JSON line per request it serves, to
`data/.state/share_gateway/access.log`: when, the visitor's address (the real
one, from the PROXY protocol header the relay's frpc stamps), the origin the
request named (an app's label host), the method, the path, the protocol, the
status, the response size and the duration. It is the owner's own record of
every request the share served, for debugging an app and for seeing a flood
of probes or refusals.

It never carries a header, a query string or a response header: the session
cookie and a visiting agent's admission token ride request headers, the login
callback's handoff token rides the query string, `Set-Cookie` rides the
response headers, and an app's own headers may carry that app's credentials.
caddy's log filter deletes all three wholesale (`User-Agent` goes with them)
and cuts the query string from the URI. Nothing in a line says who the
requester was; identity lives in the request counts below.

The log rolls at 8 MiB, keeping 4 rolled, compressed files for at most 30
days, so it never takes more than about 40 MiB. Unsharing leaves it in place;
the next share appends to it. The gateway's own process logs no request
line of its own (caddy's supersedes it); it still logs every denial.

## The request counts

The gateway counts every request caddy asks it about (`/_auth/verify`) and
writes the counts once a minute to
`$MNGR_AGENT_STATE_DIR/events/gateway_requests/events.jsonl`, the services
agent's events directory, one record per minute and key under the standard
event envelope (`type` `gateway_request_count`, `source` `gateway_requests`):

```json
{"timestamp": "2026-11-04T17:23:00.000000000Z", "type": "gateway_request_count", "event_id": "evt-...",
 "source": "gateway_requests", "window_start": "2026-11-04T17:22:00+00:00", "window_seconds": 60,
 "service": "worldbuilder", "requester_kind": "user", "requester_id": "3f1c...", "method_class": "read",
 "outcome": "allowed", "request_count": 41}
```

- `service` is the registered app name the Host resolved to (never the
  label), empty for a host that is not one of the workspace's origins.
- `requester_kind` is `owner`, `user` (a visitor with a session, whose
  `user_id` is `requester_id`), `agent` (a visiting agent, its id in
  `requester_id`), `none` (no session or token), or `overflow`.
- `method_class` is `read` (GET, HEAD, OPTIONS), `write` (any other method)
  or `websocket` (an upgrade, whatever the method).
- `outcome` is `allowed` or `denied:<reason>`, the reason one of
  `foreign_host`, `origin`, `agent_access_unconfigured`, `agent_token`,
  `no_session`, `grants_misconfigured` and `no_grant`. A redirect to the
  broker counts as `denied:no_session`; a CORS preflight of the hosted chrome
  that reaches the auth step counts as allowed by nobody.
- `timestamp` is the window's end, when the count became final; a replay of
  the stream is deduplicated by `event_id` downstream.

Never a path, a query string, an address or a header. The auth label's
`/_auth/*` and `/_agent/*` surfaces, `/_health`, the loading page, and the web
client's permission checks for an internal app's workspace routes
(`/api/workspace/...`, which caddy answers itself) are routed without the auth
step and are not counted (the access log has them).
A window keeps at most 256 distinct keys; every further key folds into one
`overflow` record, so a flood bounds itself. The stream archives itself at
32 MiB (`events.jsonl.<timestamp>.gz`, two kept), the `event_streams`
library's rule. A gateway that stops flushes every window, the open one
included; one that dies loses the windows not yet flushed, up to about two
minutes. With
`MNGR_AGENT_STATE_DIR` unset nothing is counted.

The analytics collection of an explorer-plan workspace reads this feed and
the shell's `window_opened` and `window_input` client-activity events
(`specs/minds-app-usage-signals/spec.md` in the mngr repo); the access log is
never collected. See `docs/system/analytics-collection.md`.

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
  minutes, consumed by one join, burned after five failed attempts. A request
  that relies on a session cookie, here and on the grant routes below, is held
  to the Origin policy `/_auth/verify` applies (a non-GET with a foreign
  `Origin` is answered 403), since caddy routes `/_agent/*` to the gateway
  without that step. It answers
  the code and the instruction block the inviter copies, rendered from
  `.agents/shared/references/visiting-agents-invite.md`. The scope is chosen at
  minting: `workspace` (every origin, and the routes that run a command and
  read or write a file) or a list of grantable app names (those origins
  only). The desktop mints through
  `agent-access invite` over `mngr exec`, which also works while the workspace
  is unpublished.
- **The join.** `POST /_agent/join` on the auth origin, unauthenticated and rate
  limited per client address, takes `{code, public_key, name}`: the agent's own
  Ed25519 key (generated by the helper and never leaving the agent's machine)
  and the name it chose (held to the granter-written-name rule). It writes the
  grant and answers the admission token (shown once, stored hashed) and the
  app origins the scope admits. The grant is the whole of the agent's
  authority: nothing is written outside the agent-access files. A key that
  already holds a grant re-joins: same agent id, a fresh token, the new
  invite's scope and granter.
- **Admission.** `/_auth/verify` accepts `Authorization: Bearer <token>` beside
  the session cookie. The token names a grant; the grant's scope decides which
  origins it may reach (workspace scope reaches internal apps too, an app scope
  only its apps while they are registered and not internal); the Origin policy
  is skipped, since a bearer header is never ambient; and the identity handed on
  is `{"owner":false,"agent_id":...,"agent_name":...,"granter":...}` with no
  `user_id`. Caddy strips the `Authorization` header before the backend sees
  it, as it strips the session cookie.
- **Commands and files.** Three routes on the auth origin act with the
  workspace's own authority. Each answers `Authorization: Bearer <token>`
  alone, never a session cookie, and only a token whose grant has workspace
  scope (an app-scoped grant is answered 403); the grant is looked up on every
  request, so revoking it ends access at the next one. No request is signed
  and no answer is: the token is a bearer credential with the grant's whole
  reach, which it already was, since it admits its holder to the terminal.
  A refusal is `{"error": "..."}`.
  - `POST /_agent/run` takes `{"command": ["<word>", ...], "cwd": "<dir>",
    "timeout_seconds": <number>}`. The command is run directly, not through a
    shell, with no input, as the user the gateway runs as, in `cwd` (the
    repository root when none is given; a relative one is under it). It may
    take `timeout_seconds`: 600 when none is given, never less than 1 or more
    than 3600. The answer is `application/x-ndjson`, one line per line of
    output as the command writes it, `{"type": "stdout" | "stderr", "data":
    "<the line>"}`, and a last line with the exit: `{"type": "exit", "code":
    <n>}` (`-1` for a command a signal ended), `{"type": "exit", "code": null,
    "timed_out": true}` for one that ran out of time, or `{"type": "exit",
    "code": null, "error": "..."}` for one that could not be started. The
    command is finished when it has exited and nothing it started still holds
    its output open.
  - `POST /_agent/files/read` takes `{"path": "<path>"}` and answers
    `{"exists": true, "content_b64": "<base64>"}`, or 404 with `{"exists":
    false}` for a file that is not there. A file over 48 MiB is refused, as is
    anything that is not a regular file.
  - `POST /_agent/files/write` takes `{"path": "<path>", "content_b64":
    "<base64>", "mode": "<octal digits>"}` and answers `{"written": true}`. The
    file is replaced through a temp file beside it and a rename, its directory
    is created, and its permission bits are `mode` (`600` when none is given).
    A request body may be up to 64 MiB.

  A path is taken as written when absolute and under the repository root
  otherwise; nothing confines it, since a caller that may run any command
  could reach any file that way. A command is killed with everything it
  started in its process group when its time runs out, when its caller's
  connection ends before the command does, and when the gateway stops. Nothing
  a command prints, no file's contents and no token is logged.
- **No agent's key is in `authorized_keys`.** At every start the gateway
  removes each line carrying the `imbue-agent-access agent_id=...` marker
  (`retired_agent_keys`), which a release that ran the owner-exec daemon wrote
  for an agent granted the whole workspace; every other line of the file is
  left alone. The read-and-replace holds `~/.ssh/authorized_keys.lock`, the
  lock every other writer of the file (the desktop's cross-workspace SSH
  grant) takes around its own read-and-write, so neither can overwrite the
  other's lines.
- **The empty `exec` member.** For a grant of the whole workspace, the join and
  `me` answers carry `"exec": {"origin": "", "audience": "", "host_public_key":
  ""}`; for a grant of named apps they carry no `exec`. Nothing reads the
  values. They are there for a copy of the helper from a release that ran the
  owner-exec daemon, which signs its `run`, `read` and `write` to the address
  `exec` names. With the empty values each of those fails on an address it
  cannot reach, so that copy re-reads `me`, finds that it is not the helper the
  workspace publishes, and prints its notice to reinstall, every time. With no
  `exec` at all it reads its grant as covering named apps only and says
  nothing. The helper this release publishes ignores the member, which goes
  when no such copy is left in use (`_RETIRED_EXEC_COORDINATES` in
  `agent_routes`).
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
  the script compares its own hash against, printing a notice to reinstall
  when they differ. It joins, runs commands and reads and writes files through
  the routes above with its admission token, reaches an app over the share
  with `get <app>/<path>` (the admission token as a bearer, sent to the
  workspace's origins only, waiting out an app that answers 503 with
  `Retry-After` while it starts), refreshes the skills index when its copy is
  a day old and prints what changed, and re-reads `me` and retries once when
  a request fails, except for a `run` that may already have started: that is
  reported rather than run again. A request the gateway calls wrong (a 400) is
  a usage error and is not retried. The key the helper generates at its first
  join is what names the agent to the workspace across joins; the helper signs
  nothing with it. A copy that is not the one the workspace publishes says so,
  with where to reinstall it from, each time it joins or a request of its
  fails.
- **Grants, revocation, audit.** `GET /_agent/grants` lists what the caller may
  see (the owner everything, anyone else what they granted);
  `DELETE /_agent/grants/<id>` revokes (the owner any, a granter their own).
  Invites, joins, revocations, and expiries land in
  `$MNGR_AGENT_STATE_DIR/events/agent_access/events.jsonl`, the owner's record
  of the grants' lifecycle. Nothing records what an agent ran: a workspace-scope
  agent holds the workspace's authority and can edit any file the workspace
  keeps, so such a log would prove nothing.

Grants, keys, and tokens survive an unpublish; only the tunnel goes away.
