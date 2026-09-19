# Latchkey: the user's connected accounts (rows 1 and 2)

Latchkey is a CLI tool that automatically injects credentials into curl commands.
Credentials are managed on the outside by the Mind app: sending a permission
request also triggers a login flow if necessary. No credential ever enters this
workspace; the gateway attaches it to the request.

## Builtin services (row 1)

- **Use `latchkey curl`** instead of regular `curl` for supported services. Pass
  through all regular curl arguments; latchkey is a transparent wrapper.
- **`latchkey services list`** names the builtin services. `--viable` shows the
  ones already connected or easily connectable through a browser.
- **`latchkey services info <service_name>`** gives a service's auth options,
  credential status, API docs links and special requirements.
- **Submit a permission request** (below) when `latchkey curl` fails with a
  latchkey permission error. One request per tool call, on its own, output
  untouched.

### Make an authenticated curl request

```bash
latchkey curl [curl arguments]
```

Creating a Slack channel (notice that no `Authorization` header is present):

```bash
latchkey curl -X POST 'https://slack.com/api/conversations.create' \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-channel"}'
```

Getting Discord user info:

```bash
latchkey curl 'https://discord.com/api/v10/users/@me'
```

### Interpreting latchkey permission errors

When `latchkey curl` comes back with a response like `{"error": "..."}`, it could
be a genuine error from the upstream API endpoint, but it could also be that
latchkey has not granted you permission to access the service. Inspect the error
**text** (not the status code or exit code) to decide which type of permission
request to send.

| Error latchkey returned | What it means | What to send |
| --- | --- | --- |
| `No service matches URL: <url>` | Latchkey has no service for this domain at all | `type: "custom-service"` (row 2), if the service is workable there; else rows 3 to 5 |
| `No credentials found for <service>.` | The service exists; it is not connected yet | `type: "predefined"` |
| `Request not permitted by the user.` | The service exists and is connected; you lack the permission | `type: "predefined"` |

### Ask for user permission

When there are no valid credentials for the given service, or requests come back
with "request not permitted by the user", ask the user for permission. Requests
are sent to latchkey via the reserved `latchkey-self.invalid` host:

```bash
# 1. Retrieve the list of available permissions if necessary.
latchkey curl http://latchkey-self.invalid/permissions/available/discord

# 2. Retrieve the list of your existing permissions if necessary.
latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules

# 3. Ask for the missing permissions.
# This one must go in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "predefined", "payload": {"scope": "discord-api", "permissions": ["discord-read-all"]}, "rationale": "I'"'"'d like to access your Discord account to read server and channel information so I can help you summarize conversations."}'
```

The body must be a JSON object with exactly four fields: `agent_id` (the chat the
request belongs to: use `${MINDS_CHAT_ID:-$MNGR_AGENT_ID}`, since the chat app
sets `MINDS_CHAT_ID` on every agent it creates and an agent created any other way
is its own chat), `type` (`"predefined"`), `payload`, and `rationale`.

`payload` must be an object with at least two fields: `scope` (string) and
`permissions` (array of strings). `scope` needs to be one of the scopes in the
response to the `/permissions/available/<service_name>` call. For a specific
account, add the optional third field `account` (a string), for example
`"payload": {"scope": ..., "permissions": ..., "account": "bob@example.com"}`.

When not sure (and if applicable), prefer the `*-read-all` permission variants:
they are relatively safe and obvious.

After posting, wait for an automated system message saying whether the user
approved or denied the request. If the permission still does not appear on your
first call after an approval message, sleep for a few seconds and retry; the
change can take a moment to propagate. Do not ask the user to tell you when they
respond; mention that you will continue once they do if you need to wait.

### File exactly one permission request per tool call

The chat renders each request as a card the user acts on, and builds it from that
single tool call. A second request in the same call is never shown, and anything
that keeps the echoed request object out of the call's result (a redirect, a
pipe, a backgrounded call) leaves the card with no button. A guard refuses those
shapes; file another request straight after in a tool call of its own.

### Git operations on GitHub (clone / fetch / push)

The gateway natively proxies GitHub's git smart-HTTP endpoints, so plain `git`
works through latchkey too: point git at the gateway's proxy URL and pass the
gateway's auth headers (their values are already in this environment).

```bash
git -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    ${LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE:+-c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"} \
    push "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" <refspec>
```

(`clone`, `fetch`, and `ls-remote` take the same proxy URL and headers.) The
GitHub credential is injected server-side; no token enters the container. This is
gated by the `github-git` scope: `github-git-read` covers clone and fetch,
`github-git-write` covers push. Request them like any other permission. Only
`https://github.com/<owner>/<repo>[.git]` URLs are supported; prefer one-shot `-c`
options over persisting the gateway URL or headers into git config.

### Using multiple accounts

Credentials can be associated with a specific account, and a service can hold
more than one. The user can add one from the Permissions tab of this machine's
options in the Mind app (the key icon in the tabs along the top): "Add connection"
lists the services that already have an account here under "Add another
account", and the ones that do not under "Connect a new service". You can also
send a permission request with an `account` in the payload; approving it prompts
the user to sign in. Double-check the resulting account; it may differ from the
one you requested.

Reference an account in curl calls with `latchkey --account alice@example.com
curl ...` (the `--account` option goes right after `latchkey`). The existing
accounts are the keys of the credential dictionary in `latchkey services info`;
an empty string means "unknown account".

### Expired or invalid credentials

Two ways to trigger a new login: re-send the permission request (when there is a
single account for the service), or have the user reconnect the account from the
Permissions tab ("Add connection", then "Add another account" for that service;
tell them to do that when more than one account is configured).

## Custom services (row 2)

A **custom service** is a connection to one domain latchkey has no builtin for.
The user approves it in the Mind app, pastes the credential into the approval
window (or signs in through a browser latchkey drives), and the gateway injects
it into every `latchkey curl` to that domain. The value never enters this
workspace.

Before you decide to go down this route: you do not need a connection to make
requests to URLs that require no credentials; latchkey is not necessary at all.
And a custom service covers only what the gateway can inject: a request header,
or a cookie or token captured from a browser sign-in. A key that goes in the
query string, a signature scheme, or an OAuth client is row 4.

This asks the user to create a connection to one domain and let this machine
use it:

```bash
# This one must go in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "custom-service", "payload": {"domain": "api.example.com", "scheme": "https"}, "rationale": "I'"'"'d like to reach the Example widget API to look up the part numbers you asked about."}'
```

- `domain` must be ASCII; encode non-ASCII ones with punycode.
- `scheme` must be one of `"https"` and `"http"`.

By default the user is asked to paste a token during approval, and latchkey
attaches it as an `Authorization: Bearer <token>` header. A service that takes
its key in another header names it with `payload.header`, a header line with
`{token}` where the value goes; the approval window shows the user the header the
value will be sent as:

```bash
  -d '{... "payload": {"domain": "api.example.com", "scheme": "https", "header": "X-Api-Key: {token}"}}'
```

`header` cannot be `Host` or an `X-Latchkey-*` header, and cannot be combined
with `login` (a login flow supplies its own credential shape).

Alternatively, trigger a browser sign-in flow and have latchkey retrieve and
store credentials from the browser, by adding a `login` object:

```bash
  -d '{... "payload": {"domain": "api.example.com", "scheme": "https", "login": {"url": "https://api.example.com/login", "flow": "cookie-capture", "flow_params": {"cookieKeys": ["session"]}}}}'
```

`login.url`, `login.flow` and `login.flow_params` are the same as the
`--login-url`, `--login-flow` and `--login-flow-params` flags documented in
`latchkey services register --help` (run it to see each flow's parameters), but
use this API rather than the `latchkey services register` CLI directly. All three
are required inside `login`: a flow needs a login URL and its parameters. The
parameters are checked against the flow's schema, so an unknown key is refused,
and every URL in them (`url`, `cookieUrl`, `tokenUrl`) must be on `domain` or a
subdomain of it. Every login flow has limitations: make sure the flow you request
will actually work for the service you are registering.

## When the gateway is unreachable

Every command above is routed through the latchkey gateway at
`$LATCHKEY_GATEWAY`. If it cannot be reached, treat it as a transient outage; it
usually helps if the user restarts the Mind app. Requests to `/permissions` and
`/permission-requests` are routed to the user's computer, so they fail while it
is offline.

## Notes

- All curl arguments are passed through unchanged; return code, stdout and
  stderr come back from curl.
- Unless the user explicitly asks, do not discuss latchkey or the technical
  details; it is easy for the user to get confused. Do not ask the user to run
  latchkey commands.
- Do not call `latchkey auth` commands yourself. The Mind app runs them on the
  user's computer as part of approving a permission request; for services with no
  browser auth, it provides the form the user pastes a credential into.

## Currently supported services

Latchkey offers varying levels of support for AWS, Calendly, Coolify, Discord,
Dropbox, Figma, GitHub, GitLab, Gmail, Google Analytics, Google Calendar, Google
Docs, Google Drive, Google Sheets, Google Slides, Linear, Mailchimp, Notion, Ramp,
Sentry, Slack, Stripe, Telegram, Todoist, Umami, Yelp, Zoom, and more. The list
that counts is `latchkey services list`.
