# The service's MCP server (row 3)

## When this row applies

Row 3 is a server the service itself publishes: its docs link to it, or it
lives in the service's own GitHub organization. A listing in an MCP directory or
registry does not make a server official; anyone can list one. Local or hosted,
it is equally fine: the service already holds the data it passes on.

A server anyone else publishes is not a row. Run here, it is a stranger's code
with this workspace's access and the user's key; hosted, whoever runs it sees
the key and every request. Its source can still show you which endpoints and
auth scheme the service's API takes, for row 2 or row 5.

MCP is allowed for a service latchkey has no builtin for. The workspace disables
claude.ai's own connector sync (`ENABLE_CLAUDEAI_MCP_SERVERS=false` in
`.mngr/settings.toml`) because that is an OAuth side channel around the latchkey
gateway for services latchkey *does* support; a server for a service it does not
support bypasses nothing. Many of latchkey's builtins are themselves remote MCP
servers (`notion-mcp`, `atlassian-mcp`, `airtable-mcp`, ...): those are row 1,
called as "Calling a hosted server" below says.

## Where the connection lives

**A hosted server (a URL) is a latchkey custom service.** Its sign-in (the
server's own OAuth, through the `remote-mcp` login flow) or its key (a header)
stays in Imbue Studio's credential store; the gateway attaches it to every
request and refreshes an OAuth token itself; and the connection is there at
once for every chat on every harness, for an app, and for a scheduled job.

Never wire a hosted server into a harness's own MCP config instead (`claude mcp
add`, `.mcp.json`, codex's `config.toml`, and the like), and never sign in to
one with `mcpc login`. A sign-in held that way reaches only the one client that
holds it, loads only when that client next starts, sits in a store Imbue Studio
does not manage, and the user repeats it per harness and whenever it expires.

**A server that runs here (a package) goes through `mcpc`**, the workspace's
MCP client, with its key through the secret card: the gateway cannot inject a
credential into a local process. See "A server that runs here" below.

## A hosted server: ask latchkey for the connection

### Find the server's endpoints

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
  `registration_endpoint`**: the `remote-mcp` login flow, below. Note
  `scopes_supported`; request only the scopes the task needs, none when the
  server lists none.
- **`401` and no `registration_endpoint`**: the server wants a client someone
  registered by hand, which no flow here can do. Row 3 is out; go on to row 5
  or 6.
- **The docs say the server takes an API key in a header**: a custom service
  with a key, "A hosted server with a key" below.
- **No `401`, no key**: nothing to connect. Call the endpoint directly, with
  plain `curl` as below or an `mcpc` entry with only a `url`.

### The `remote-mcp` login flow

Send a `custom-service` permission request (`references/latchkey.md`, "Custom
services") whose `domain` is the MCP endpoint's host and whose `login` is the
flow. `login.url` is the authorization endpoint, which is where the flow sends
the user anyway; `login.flow_params` carries the endpoints, the `scopes`, and
`issuesRefreshTokens: false` only when the server's docs say it issues access
tokens alone. The URLs in the parameters have to be on `domain` or a subdomain
of it, as for the other flows.

```bash
# This one must go in a tool call of its own, with nothing else in it and its output untouched.
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "custom-service", "payload": {"domain": "mcp.example.com", "scheme": "https", "login": {"url": "https://mcp.example.com/authorize", "flow": "remote-mcp", "flow_params": {"mcpEndpoint": "https://mcp.example.com/mcp", "authorizationEndpoint": "https://mcp.example.com/authorize", "tokenEndpoint": "https://mcp.example.com/token", "registrationEndpoint": "https://mcp.example.com/register", "scopes": ["read"]}}}, "rationale": "Example has its own connector; I'"'"'d like to use it to read the projects you asked about."}'
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
the request.

### A hosted server with a key

A server that takes its key in a header is row 2's request on the MCP
endpoint's domain: the default bearer header or the one the docs name in
`payload.header`, with `payload.credential_instructions` saying where the key
comes from. The gateway attaches it to every call below.

### Calling a hosted server

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

For a long run of calls, `mcpc` can hold a session to the server through the
gateway instead: an entry whose `url` is the gateway's proxy of the endpoint,
carrying the gateway's own headers (`${VAR}` expands from the environment, where
these already are, so no wrapper is needed), and the credential is still
attached by the gateway.

```json
"example": {
  "url": "${LATCHKEY_GATEWAY}/gateway/https://mcp.example.com/mcp",
  "headers": {"X-Latchkey-Gateway-Password": "${LATCHKEY_GATEWAY_PASSWORD}"}
}
```

Connect it as in "A server that runs here" below. When the connect fails on
the server's response stream, stay with `latchkey curl`.

## A server that runs here: mcpc

Even the service's own server, run here, is code with this workspace's access,
including every variable the wrapper hands it; the secrets guard stops your own
tool calls, not a process. So install the version you looked at, not whatever
is newest at each launch. `npm view <package> version` gives an npm package's
current version, and its pypi.org page gives a Python package's; the command
names that version (`npx -y @example/mcp-server@1.2.3`,
`uvx example-mcp@1.2.3`). To move to a newer version, look at what changed and
edit the pin.

Every such server is reached through `mcpc`, never through a harness's own
MCP config (`.mcp.json`, codex's `config.toml`, and the like). A server wired
into one harness reaches only that harness, loads only when its chat next
starts, and keeps its key in that harness's store; one connected through mcpc
is usable at once, from any chat on any harness, from an app, and from a
scheduled job.

- **`mcp-servers.json`** at the repo root lists the servers, in the standard
  `mcpServers` shape. It holds no secret: a key is named as `${VAR}`, and a file
  holding one is named by its path.
- **A session per server**, named `@<name>` after its entry. mcpc keeps it in a
  background process and brings it back on the next call after a restart.
- **`~/.mcpc`** links to `data/.secrets/mcpc/`, where mcpc keeps its sessions
  and the headers it sends. The secrets guard covers the directory; reach it
  only through `mcpc`.

**Check what is already connected first:** `mcpc` with no arguments lists every
session and its state (`live`, `crashed`, `unauthorized`, ...). A `live` or
`crashed` session for the service is ready to use as it is.

### Adding a server

Add an entry to `mcp-servers.json` with your file-editing tool (a shell
command that writes a `data/.secrets` path is refused by the guard). Paths are
absolute: mcpc restarts a server from whatever directory the next caller runs
in, so a relative path breaks the first time an app or job calls it.

A server that runs here, needing no credential:

```json
"example": {
  "command": "npx",
  "args": ["-y", "@example/mcp-server@1.2.3"]
}
```

A server that runs here and needs a key: request the key through the secret card
(below), then run the server under the wrapper, which puts the file's variables
into the server's environment. mcpc hands a server only a minimal environment
(`PATH`, `HOME`, ...); a non-secret variable it needs goes in the entry's `env`.

```json
"example": {
  "command": "python3",
  "args": ["/home/user/workspace/system/scripts/with_secrets.py", "/home/user/workspace/data/.secrets/example.env", "--", "npx", "-y", "@example/mcp-server@1.2.3"]
}
```

A hosted server that needs no credential at all is a `url` entry:

```json
"example": {"url": "https://mcp.example.com/mcp"}
```

Then open the session. From the repo root:

```bash
mcpc connect mcp-servers.json:example @example
```

`connect` prints the server's tools. An older entry of a hosted server with a
`headers` value naming `${VAR}` and a `secretsFile` (the file that connect runs
under, which a published template declares) connects under the wrapper, naming
that file; when it warns `Environment variable not found`, the connect ran
without the wrapper and the header went out empty: close the session and
connect again under it. A new connection to a hosted server goes through
latchkey instead, as above.

```bash
python3 system/scripts/with_secrets.py data/.secrets/example.env -- mcpc connect mcp-servers.json:example @example
```

### Using it

```bash
mcpc @example tools-list                           # the tools, one line each
mcpc @example tools-get search                     # one tool's full input schema
mcpc @example tools-call search query:="invoices"  # key:=value, JSON-typed
mcpc --json @example tools-call search '{"query": "invoices", "limit": 5}'
```

`--json` prints the MCP result object, and exits non-zero when the call fails
(a tool's own error comes back with `"isError": true`); that is the form an app
or a scheduled job uses, from any language, by running the command. The
server's tools are yours as soon as the connect returns: there is nothing to
restart.

A session mcpc keeps runs a background process of about 100 MB for as long as the
session is open. Keep sessions for a service the user will keep using; close one
you opened for a single task.

### Changing or removing a server

`connect` on a session that is already open changes nothing. To pick up a new
key, a changed entry, or a new pin, run `mcpc @example close` and connect again
(under the wrapper when the entry runs under one). To remove a server, close
its session and delete its entry.

### The key goes through the secret card

Never put a key in `mcp-servers.json`, on an `mcpc` command line (`--header`
with a value), or in a command you run. Request it:

```bash
python3 .agents/skills/connect-external-service/scripts/request_secret.py \
  --file example --var EXAMPLE_API_KEY --rationale "The Example connector needs your API key to read your projects."
```

End the turn; when `Secret stored: data/.secrets/example.env (EXAMPLE_API_KEY) ...`
arrives, add the entry and connect as above.

## What to tell the user

Which service you are connecting, that it needs a sign-in (or a key) and where
it goes, and that it stays in Imbue Studio's credential store (or, for a server
run here, in this workspace) for the connection to use. Never the file path,
the config file, latchkey, mcpc, or the word MCP, unless they ask.
