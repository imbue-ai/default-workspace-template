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

A server gets its credential one of three ways: an API key, which goes through
the secret card (below); nothing at all, for a server that needs none; or its
own OAuth sign-in, whose consent screen needs a browser you hand to the user
(last section). For the service's own server none of these outranks another; a
community server takes a key or nothing, as above.

## The key goes through the secret card, and the server reads it through the wrapper

Never put a key in the config file. Request it, then wrap the server command:

```bash
python3 .agents/skills/connect-external-service/scripts/request_secret.py \
  --file example --var EXAMPLE_API_KEY --rationale "The Example MCP server needs your API key to read your projects."
```

End the turn; when `Secret stored: data/.secrets/example.env (EXAMPLE_API_KEY) ...`
arrives, wire the server with `with_secrets.py` in front of it. The wrapper puts
the file's variables into the server's environment and execs it, so the config
names only the file and the variable, never the value. Do this on every harness:
Claude expands `${VAR}` in `.mcp.json` from the harness process environment,
which does not hold the secret, and codex's `env` table holds literal strings, so
neither substitutes the value for you.

## Per-harness wiring

Wire the harness the chat runs on: the one whose config you are running under
(`echo $CODEX_HOME` is set in a codex chat's shell and empty elsewhere; the
chat's model bar names the harness too).

**Claude Code** ([docs](https://code.claude.com/docs/en/mcp)): project-scope
`.mcp.json` at the repo root. `.claude/settings.json` sets
`enableAllProjectMcpServers`, so a server added here is approved without a prompt
in the terminal pane; a new server is picked up when the chat's agent restarts
(ask the user to restart the chat, or wait for the next one).

```json
{
  "mcpServers": {
    "example": {
      "command": "python3",
      "args": ["system/scripts/with_secrets.py", "data/.secrets/example.env", "--", "npx", "-y", "@example/mcp-server@1.2.3"]
    }
  }
}
```

**Codex** ([docs](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)):
`config.toml` under the chat's `CODEX_HOME` (each codex agent runs with its own,
under its agent state dir; `echo $CODEX_HOME` prints it). Add a
`[mcp_servers.<name>]` table with `command` and `args` in the same wrapped shape;
`env` values there are literal, which is why the wrapper carries the key.

```toml
[mcp_servers.example]
command = "python3"
args = ["system/scripts/with_secrets.py", "data/.secrets/example.env", "--", "npx", "-y", "@example/mcp-server@1.2.3"]
```

**pi** ([pi-mcp-adapter](https://pi.dev/packages/pi-mcp-adapter)): pi has no
built-in MCP client; the adapter (`pi install npm:pi-mcp-adapter`) reads the
project's `.mcp.json`, so the Claude entry above serves pi too once the adapter is
installed.

**Antigravity** ([docs](https://antigravity.google/docs/mcp?tab=cli)): the
workspace-local `.agents/mcp_config.json` (or `~/.gemini/config/mcp_config.json`),
a `mcpServers` object of `command` / `args` / `env` in the same wrapped shape.

**A hosted server** (a URL rather than a package) is wired the same way, through
[mcp-remote](https://github.com/geelen/mcp-remote), a bridge that runs here,
pinned like any other server, and forwards to the URL:

```json
"args": ["system/scripts/with_secrets.py", "data/.secrets/example.env", "--", "npx", "-y", "mcp-remote@0.14.3", "https://mcp.example.com/mcp", "--header", "Authorization: Bearer ${EXAMPLE_API_KEY}"]
```

mcp-remote fills in `${EXAMPLE_API_KEY}` itself, from the environment the wrapper
gives it. Claude Code leaves it as written because its own environment does not
hold the variable (`claude mcp list` warns about that; the server still starts).
A hosted server that needs no key drops the wrapper and the `--header`; one with
its own OAuth sign-in prints its consent URL through mcp-remote, as below.

Wiring is built and tested only for the harnesses this workspace has an account
for; do not write a config for a harness the user does not run.

## An OAuth sign-in, in a browser the user can see

A server that signs in through OAuth opens a consent page and listens on a
`localhost` callback. That callback must resolve inside this workspace, so the
browser has to run here too: start one with the `agentic-browser-fleet` skill,
navigate to the consent URL the server prints, and `handoff` the browser so the
user completes the sign-in themselves. Say what you are doing: "Example needs
you to sign in once; I've opened a browser you can take over."

## What to tell the user

Which service's server you are wiring, that it needs a key (or a sign-in) and
where it goes, and that the key stays on this workspace's disk for the server to
use. Never the file path, the config file, or the word MCP, unless they ask.
