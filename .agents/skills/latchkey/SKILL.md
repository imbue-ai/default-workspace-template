---
name: latchkey
description: Use whenever you want to use latchkey commands or interact with third-party or self-hosted services (Slack, Google Workspace, Dropbox, GitHub, Linear, Coolify...) using their HTTP APIs on the user's behalf.
compatibility: Requires node.js, curl and latchkey (npm install -g latchkey).
metadata:
  author: imbue
---

# Latchkey

## Instructions

Latchkey is a CLI tool that automatically injects credentials into curl commands. Credentials are managed on the outside by the Minds app - sending a permission request also triggers a login flow if necessary.

Use this skill when the user asks you to work on their behalf with services that have HTTP APIs, like AWS, GitLab, Google Drive, Discord or others.

Usage:

- **Use `latchkey curl`** instead of regular `curl` for supported services.
  - Pass through all regular curl arguments - latchkey is a transparent wrapper.
- Latchkey comes with a list of builtin supported services.
  - **Check for `latchkey services list`** to get a list of supported services. Use `--viable` to only show the currently configured ones.
  - **Use `latchkey services info <service_name>`** to get information about a specific service (auth options, credentials status, API docs links, special requirements, etc.).
  - You can request to register more custom services, although the options for authenticating to custom services are limited. See "Ask for a new connection to a domain latchkey does not know" below.
- **Submit a permission request to the user** by calling `latchkey curl -XPOST http://latchkey-self.invalid/permission-requests` when `latchkey curl` fails with a Latchkey permission error. See "Interpreting Latchkey permission errors" below. One request per tool call, on its own, output untouched.
- **Look for the newest documentation of the desired public API online.** Avoid bot-only endpoints.


## Examples

### Make an authenticated curl request
```bash
latchkey curl [curl arguments]
```

### Creating a Slack channel
```bash
latchkey curl -X POST 'https://slack.com/api/conversations.create' \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-channel"}'
```

(Notice that `-H 'Authorization: Bearer` is not present in the invocation.)

### Getting Discord user info
```bash
latchkey curl 'https://discord.com/api/v10/users/@me'
```

### Interpreting Latchkey permission errors

When `latchkey curl` comes back with a response like `{"error": "..."}`,
it could be a genuine error from the upstream API endpoint, but it could also be
that Latchkey hasn't granted you permission to access the service. When the
latter is true, you can call
`latchkey curl -XPOST http://latchkey-self.invalid/permission-requests`
to request the user to grant you the necessary permission.

Inspect the error **text** (not the status code or exit code) to decide which
type of permission request to send.

| Error latchkey returned | What it means | What to send |
| --- | --- | --- |
| `No service matches URL: <url>` | Latchkey has no service for this domain at all | `type: "custom-service"` |
| `No credentials found for <service>.` | The service exists; it is not connected yet | `type: "predefined"` |
| `Request not permitted by the user.` | The service exists and is connected; you lack the permission | `type: "predefined"` |

### Ask for a new connection to a domain latchkey does not know

Before you decide to go down this route, note that:

- You don't need to ask for a new connection to make requests to URLs that don't
  require credentials. Latchkey is not necessary at all.

- The authentication headers and login flows currently supported by Latchkey are
  limited (see details below ). If the service needs anything else, fall back to
  either:

   - Ask the user to perform some operations manually.
   - If the user really wants automated access, ask the user to type in the
     credentials and store them within the workspace, with ample warning
     about its security consequence of that.

This asks the user to create a connection to one domain and to let this machine
use it:

```bash
# This one must go in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "custom-service", "payload": {"domain": "api.example.com", "scheme": "https"}, "rationale": "I'"'"'d like to reach the Example widget API to look up the part numbers you asked about."}'
```

- `domain` must be ASCII; encode non-ASCII ones with punycode.
- `scheme` must be one of `"https"` and `"http"`.

By default the user is asked to paste a token during approval, and Latchkey
attaches it as an `Authorization: Bearer <token>` header.

Alternatively, trigger a browser sign-in flow and have Latchkey retrieve and
store credentials from the browser, by specifying a `login_flow` and
`login_flow_params`.

```bash
  -d '{... "payload": {"domain": "api.example.com", "scheme": "https", "login_url": "https://api.example.com/login", "login_flow": "cookie-capture", "login_flow_params": {"cookieKeys": ["session"]}}}'
```

The `login_url`, `login_flow` and `login_flow_params` fields are the same as the
`--login-url`, `--login-flow` and `--login-flow-params` flags documented in
`latchkey services register --help` (run it to see each flow's parameters), but
beware that you should use this API instead of the `latchkey services register`
CLI directly. The three go together: a flow needs a login URL and its
parameters. The parameters are checked against the flow's schema, so an unknown
key is refused, and every URL in them (`login_url`, `cookieUrl`, `tokenUrl`)
must be on `domain` or a subdomain of it.

Furthermore, beware that every login flow has limitations: make sure that the
login flow you request will actually work for the custom service you're
registering.

### Ask for user permission

When either there are no valid credentials for the given service or our
requests come back with the "request not permitted by the user"
message, ask the user for permission. The requests are sent to
Latchkey via the reserved `latchkey-self.invalid` host:

```bash
# 1. Retrieve the list of available permissions if necessary.
latchkey curl http://latchkey-self.invalid/permissions/available/discord

# 2. Retrieve the list of your existing permissions if necessary.
latchkey curl http://latchkey-self.invalid/permissions/self | jq .rules

# 3. Ask for the missing permissions.
# This one must go in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "predefined", "payload": {"scope": "discord-api", "permissions": ["discord-read-all"]}, "rationale": "I'"'"'d like to access your Discord account to read server and channel information so I can help you summarize conversations."}'
```

The body must be a JSON object with exactly four fields:
`agent_id` (use `$MNGR_AGENT_ID`), `type` (use "predefined"), `payload`, and `rationale`.

`payload` must be an object with at least two fields: `scope` (string) and `permissions` (array of strings). `scope` needs to be one of the scopes specified in the response to the `/permissions/available/<service_name>` call.

When you need permissions for a specific account, you can
specify the optional third field on the `payload`: `account` (which should be a string).
For example: `-d '{... "payload": {"scope": ..., "permissions": ..., "account": "bob@example.com"}}'.

When not sure (and if applicable), prefer the `*-read-all` permission variants as they are relatively safe and obvious.

After posting, wait for an automated system message indicating
whether the user approved or denied the permission request. If
the permission still doesn't appear on your first call after an
approval message, sleep for a few seconds and retry - the change
can take a moment to propagate.

Do not ask the user to tell you when they respond to a request. Just mention
that you'll continue once they do if that's something you need to wait on.


### Git operations on GitHub (clone / fetch / push)

The gateway natively proxies GitHub's git smart-HTTP endpoints, so plain
`git` works through latchkey too: point git at the gateway's proxy URL and
pass the gateway's auth headers (their values are already in this
environment).

```bash
git -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    ${LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE:+-c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"} \
    push "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" <refspec>
```

(`clone`, `fetch`, and `ls-remote` take the same proxy URL and headers.) The
GitHub credential is injected server-side -- no token enters the container.
This is gated by the `github-git` scope: `github-git-read` covers clone and
fetch, `github-git-write` covers push. Request them like any other permission
(see "Ask for user permission" above). Only `https://github.com/<owner>/<repo>[.git]`
URLs are supported; prefer one-shot `-c` options over persisting the gateway
URL or headers into git config.

### List usable services

```bash
latchkey services list --viable
```

Lists services that either have stored credentials or can be easily authenticated into via a browser.

### Get service-specific info
```bash
latchkey services info slack
```

Returns auth options, credentials status, and developer notes about the service.

### Using multiple accounts

It is possible to associate credentials with a specific account
(and have credentials for more than a single account per service).
The user can do that from the Permissions tab of this machine's options in the
Minds app (the key icon in the tabs along the top): "Add connection" lists the
services that already have an account here under "Add another account", and the
ones that do not under "Connect a new service".

Another way is for you to send a permission request with an "account"
in the payload as described above - approving the permission request will prompt
the user to sign in. Just double-check the actual resulting account; it may be
different than the one requested by you.

You can then reference it in curl calls:

```bash
latchkey --account alice@example.com curl ...
```

The `--account` option must go right after `latchkey`.

You can see the existing accounts as keys in the credential
dictionary produced by `latchkey services info`. An empty string
as the key means "unknown account".

### Expired or invalid credentials

When the existing credentials are expired or invalid, there are currently two ways  to trigger a new login:

- By re-sending the permission request to the user (use this when there's just a single account for the given service)
- By having the user reconnect the account from the Permissions tab of this machine's options in the Minds app (the key icon in the tabs along the top): "Add connection" then "Add another account" for that service. Tell the user to do that if there is more than one account configured for the given service.


## When the gateway is unreachable

Every command above is routed through the Latchkey gateway at
`$LATCHKEY_GATEWAY`. If it cannot be reached, treat it as
a transient outage. It usually helps if the user restarts the
Minds app. Requests to /permissions and /permission-requests are
routed to the user's computer so they will fail if it's offline.


## Notes

- All curl arguments are passed through unchanged
- Return code, stdout and stderr are passed back from curl
- Unless the user explicitly asks about it, don't discuss Latchkey or the technical details (it's easy for the user to get confused).
- Do not ask the user to run Latchkey commands.
- Do not explicitly call `latchkey auth` commands! They are run automatically by the Minds app on the user's computer as part of the permission request approval process. Even for services that do not support browser auth, the Minds app usually provides an interface for the user to paste manually obtained credentials (e.g. an API key).


## Currently supported services

Latchkey currently offers varying levels of support for the
following services: AWS, Calendly, Coolify, Discord, Dropbox, Figma, GitHub, GitLab,
Gmail, Google Analytics, Google Calendar, Google Docs, Google Drive, Google Sheets, Google Slides,
Linear, Mailchimp, Notion, Ramp, Sentry, Slack, Stripe, Telegram, Todoist, Umami, Yelp, Zoom, and more.
