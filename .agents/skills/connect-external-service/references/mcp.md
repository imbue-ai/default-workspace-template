# The service's MCP server (row 3)

## When this row applies

Row 3 is a hosted MCP server the service itself publishes: its docs link to
it, or it lives in the service's own GitHub organization. A listing in an MCP
directory or registry does not make a server official; anyone can list one.

A server anyone else hosts is not a row: whoever runs it sees the user's
credential and every request. Its source can still show you which endpoints
and auth scheme the service's API takes, for row 2 or row 5.

A server published as a package to run here is not a row either. It only
calls the service's API with a key, and nothing it adds is worth running a
stranger's code with this workspace's access: use that API through row 2 or
row 5 instead, and read the package's source for the endpoints it calls.

MCP is allowed for a service latchkey has no builtin for. The workspace disables
claude.ai's own connector sync (`ENABLE_CLAUDEAI_MCP_SERVERS=false` in
`.mngr/settings.toml`) because that is an OAuth side channel around the latchkey
gateway for services latchkey *does* support; a server for a service it does not
support bypasses nothing. Many of latchkey's builtins are themselves remote MCP
servers (`notion-mcp`, `atlassian-mcp`, `airtable-mcp`, ...): those are row 1,
called as "Calling the server" below says.

## The connection is a latchkey custom service

The server's sign-in (its own OAuth, through the `mcp` login flow) or its key
(a header) stays in Imbue Studio's credential store; the gateway attaches it to
every request and refreshes an OAuth token itself; and the connection is there
at once for every chat on every harness, for an app, and for a scheduled job.

Never wire the server into a harness's own MCP config instead (`claude mcp
add`, `.mcp.json`, codex's `config.toml`, and the like), and never sign in to
it with an MCP client of your own. A sign-in held that way reaches only the one
client that holds it, loads only when that client next starts, sits in a store
Imbue Studio does not manage, and the user repeats it per harness and whenever
it expires.

## Find the server's endpoints

The MCP endpoint comes from the service's docs. How it authenticates comes from
the server itself, with no credential: a Streamable HTTP server answers an
unauthenticated `initialize` with `401` and a `WWW-Authenticate` header whose
`resource_metadata` URL (usually `/.well-known/oauth-protected-resource`) lists
its `authorization_servers`; each of those publishes its endpoints at
`/.well-known/oauth-authorization-server`. Most hosted servers serve both at
the MCP endpoint's origin.

```bash
curl -sS -D - -o /dev/null -X POST https://mcp.example.com/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
curl -sS https://mcp.example.com/.well-known/oauth-protected-resource
curl -sS https://mcp.example.com/.well-known/oauth-authorization-server
```

What you find decides the request:

- **`401` and metadata with `authorization_endpoint`, `token_endpoint` and
  `registration_endpoint`**: the `mcp` login flow, below. Note
  `scopes_supported`: they go in the request, and there is no adding one
  later (see the flow).
- **`401` and no `registration_endpoint`**: the server wants a client someone
  registered by hand, which no flow here can do. Row 3 is out; go on to row 5
  or 6.
- **The docs say the server takes an API key in a header**: a custom service
  with a key, "A server with a key" below.
- **No `401`, no key**: nothing to connect. Call the endpoint with plain
  `curl`, as below but without latchkey.

## The `mcp` login flow

Send a `custom-service` permission request (`references/latchkey.md`, "Custom
services") whose `domain` is the MCP endpoint's host and whose `login` is the
flow. `login.url` is the OAuth authorization endpoint; `login.flow_params`
carries the MCP endpoint, the token and registration endpoints, the `scopes`,
and `issuesRefreshTokens: false` only when the server's docs say it issues
access tokens alone. The URLs in the parameters have to be on `domain` or a
subdomain of it, as for the other flows.

**Err on the side of too many scopes.** A connection's scopes are fixed when
the user signs in, and there is no request that adds one: a scope that turns
out to be missing later means asking the user to disconnect the service in
Imbue Studio (the Permissions tab of this machine's options) and then setting
it up again from scratch, sign-in included. So ask for the broad scope when
the server offers one (its `scopes_supported`, or the server's docs), and
otherwise for every scope the service could plausibly need for this user's
work, not just the one the current task needs. Ask for none only when the
server lists none.

```bash
# This one must go in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "custom-service", "payload": {"domain": "mcp.example.com", "scheme": "https", "login": {"url": "https://mcp.example.com/authorize", "flow": "mcp", "flow_params": {"mcpEndpoint": "https://mcp.example.com/mcp", "tokenEndpoint": "https://mcp.example.com/token", "registrationEndpoint": "https://mcp.example.com/register", "scopes": ["read", "write"]}}}, "rationale": "Example has its own connector; I'"'"'d like to use it to read the projects you asked about."}'
```

Approving opens latchkey's own browser on the user's computer at the server's
consent page, where the app asking may be named after latchkey or Imbue Studio
rather than after you; say so if the user asks. The sign-in and the OAuth
client it registered stay in Imbue Studio's credential store, and the gateway
refreshes the token itself. A login that fails because the server issued no
refresh token is sent again with `"issuesRefreshTokens": false`; such a token
expires for good, and the user signs in again then.

After approval the service is one `latchkey services list` names, and a later
`No credentials found` or `Request not permitted by the user` is handled as for
a builtin (`references/latchkey.md`, "Interpreting latchkey permission
errors"). A `401` from the server after that means the sign-in is gone; re-send
the request. A `403` or an `insufficient_scope` error means the connection's
scopes do not cover the call: tell the user plainly that the connection has to
be set up again with wider access, ask them to disconnect the service in Imbue
Studio, and once they have, send the request again with the scopes it needs.

## A server with a key

A server that takes its key in a header is row 2's request on the MCP
endpoint's domain: the default bearer header or the one the docs name in
`payload.header`, with `payload.credential_instructions` saying where the key
comes from. The gateway attaches it to every call below.

## Calling the server

A remote MCP server is a JSON-RPC API over HTTP, so `latchkey curl` calls it
like any other. Every message is a `POST` to the MCP endpoint with both accept
types (a server answers `406` otherwise):

```bash
latchkey curl -sS -X POST https://mcp.example.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-06-18' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

```bash
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"invoices","limit":5}}}'
```

The body is either one JSON-RPC message or an SSE stream whose `data:` lines
each carry one; the last parseable one is the response. A tool's own failure
comes back as a result with `"isError": true`. Most hosted servers accept
these stateless calls; one that insists on a session rejects them with an
error naming it, so send `initialize` first with `-D -` to read the
`Mcp-Session-Id` response header, send `notifications/initialized`, and pass
`-H 'Mcp-Session-Id: <id>'` on every later call. An app or a scheduled job
makes the same calls through `latchkey curl`.

## What to tell the user

Which service you are connecting, that it needs a sign-in (or a key) and where
it goes, and that it stays in Imbue Studio's credential store for the
connection to use. Never latchkey or the word MCP, unless they ask.
