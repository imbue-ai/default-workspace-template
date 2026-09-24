# The service's MCP server (rows 3 and 5)

## When these rows apply

Row 3 is a server the service itself publishes: its docs link to it, or it
lives in the service's own GitHub organization. A listing in an MCP directory or
registry does not make a server official; anyone can list one. Local or hosted,
it is equally fine: the service already holds the data it passes on.

Row 5 is anyone else's server, taken only after a key (row 2) and a sign-in
(row 4) are ruled out, and only when it is actively maintained and needs no more
than a key the user can copy. Run it here, at a pinned version. A community
server someone else hosts is the last resort within the row: whoever runs it
sees the key and every request.

A server that runs here is code with this workspace's access, including every
variable the wrapper hands it; the secrets guard stops your own tool calls, not
a process. So install the version you looked at, not whatever is newest at each
launch. `npm view <package> version` gives an npm package's current version, and
its pypi.org page gives a Python package's; the command names that version
(`npx -y @example/mcp-server@1.2.3`, `uvx example-mcp@1.2.3`). To move to a
newer version, look at what changed and edit the pin.

MCP is allowed for a service latchkey has no builtin for. The workspace disables
claude.ai's own connector sync (`ENABLE_CLAUDEAI_MCP_SERVERS=false` in
`.mngr/settings.toml`) because that is an OAuth side channel around the latchkey
gateway for services latchkey *does* support; a server for a service it does not
support bypasses nothing.

## One client for the whole workspace: mcpc

Every MCP server is reached through `mcpc`, the workspace's MCP client, never
through a harness's own MCP config (`.mcp.json`, codex's `config.toml`, and the
like). A server wired into one harness reaches only that harness, loads only
when its chat next starts, and keeps its sign-in in that harness's store; one
connected through mcpc is usable at once, from any chat on any harness, from an
app, and from a scheduled job.

- **`mcp-servers.json`** at the repo root lists the servers, in the standard
  `mcpServers` shape. It holds no secret: a key is named as `${VAR}`, and a file
  holding one is named by its path.
- **A session per server**, named `@<name>` after its entry. mcpc keeps it in a
  small background process and brings it back on the next call after a restart.
- **`MCPC_HOME_DIR`** points at `data/.secrets/mcpc/`, where mcpc keeps its
  sessions, the OAuth sign-ins it holds, and the headers it sends. The secrets
  guard covers the directory; reach it only through `mcpc`.

**Check what is already connected first:** `mcpc` with no arguments lists every
session and its state (`live`, `crashed`, `unauthorized`, ...). A `live` or
`crashed` session for the service is ready to use as it is.

## Adding a server

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

A hosted server (a URL rather than a package) that needs no credential, or signs
in with its own OAuth (sign in before connecting; see the last section):

```json
"example": {"url": "https://mcp.example.com/mcp"}
```

A hosted server that takes a key as a header names the variable, never the
value; the value comes from the environment of the `connect` below:

```json
"example": {
  "url": "https://mcp.example.com/mcp",
  "headers": {"Authorization": "Bearer ${EXAMPLE_API_KEY}"}
}
```

Then open the session. From the repo root:

```bash
mcpc connect mcp-servers.json:example @example
```

For a hosted server with a header, run the connect under the wrapper, so mcpc
reads the variable; it stores the header with the session and sends it from then
on:

```bash
python3 system/scripts/with_secrets.py data/.secrets/example.env -- mcpc connect mcp-servers.json:example @example
```

`connect` prints the server's tools. When it warns `Environment variable not
found`, the connect ran without the wrapper and the header went out empty: close
the session and connect again under the wrapper.

## Using it

```bash
mcpc @example tools-list                           # the tools, one line each
mcpc @example tools-get search                     # one tool's full input schema
mcpc @example tools-call search query:="invoices"  # key:=value, JSON-typed
mcpc --json @example tools-call search '{"query": "invoices", "limit": 5}'
```

`--json` prints the MCP result object, and exits non-zero when the call fails
(a tool's own error comes back with `"isError": true`); that is the form an app
or a scheduled job uses, from any language, by running the command. The server's tools are yours as soon as the connect returns: there
is nothing to restart.

A session mcpc keeps runs a background process of about 100 MB for as long as the
session is open. Keep sessions for a service the user will keep using; close one
you opened for a single task.

## Changing or removing a server

`connect` on a session that is already open changes nothing. To pick up a new
key, a changed entry, or a new pin, run `mcpc @example close` and connect again
(under the wrapper when the entry has a header). To remove a server, close its
session, `mcpc logout <url>` if it signed in, and delete its entry. A session in
the `unauthorized` state needs the sign-in again, then `mcpc @example restart`.

## The key goes through the secret card

Never put a key in `mcp-servers.json`, on an `mcpc` command line (`--header`
with a value), or in a command you run. Request it:

```bash
python3 .agents/skills/connect-external-service/scripts/request_secret.py \
  --file example --var EXAMPLE_API_KEY --rationale "The Example connector needs your API key to read your projects."
```

End the turn; when `Secret stored: data/.secrets/example.env (EXAMPLE_API_KEY) ...`
arrives, add the entry and connect as above.

## An OAuth sign-in, in a browser the user can see

`mcpc login <url>` signs mcpc in to a hosted server. It prints an
`Authorization URL:` line and waits for the consent page to redirect to
`127.0.0.1:13316` in this workspace, so the browser has to run here too. It
also offers to read a pasted redirect from its input and gives up the moment
that input closes, so keep the input open and run it in the background:

```bash
mkdir -p data/.tasks/mcp-login
(sleep 900 | mcpc login https://mcp.example.com/mcp > data/.tasks/mcp-login/example.out 2>&1 &)
```

Read the URL from that file, start a browser with the `agentic-browser-fleet`
skill, navigate to the URL, and `handoff` the browser so the user completes the
sign-in themselves. Say what you are doing: "Example needs you to sign in once;
I've opened a browser you can take over." The consent page names the requester
as mcpc. When the user hands the browser back, the file ends with the login's
result; then connect as above, and the session uses the saved sign-in and
refreshes it itself.

## What to tell the user

Which service you are connecting, that it needs a key (or a sign-in) and where it
goes, and that the key stays in this workspace for the connection to use. Never
the file path, the config file, mcpc, or the word MCP, unless they ask.
