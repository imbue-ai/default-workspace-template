# The service's MCP server (row 3)

## When this row applies

MCP is allowed for a service latchkey has no builtin for. The workspace disables
claude.ai's own connector sync (`ENABLE_CLAUDEAI_MCP_SERVERS=false` in
`.mngr/settings.toml`) because that is an OAuth side channel around the latchkey
gateway for services latchkey *does* support; a server for a service it does not
support bypasses nothing.

Prefer, in order: a server authenticated by an API key; a local stdio server
that needs no credential; a server with its own OAuth sign-in. The first two
take their key through the secret card (below). The third needs a browser for
the consent screen, which you hand to the user (last section).

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
      "args": ["system/scripts/with_secrets.py", "data/.secrets/example.env", "--", "npx", "-y", "@example/mcp-server"]
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
args = ["system/scripts/with_secrets.py", "data/.secrets/example.env", "--", "npx", "-y", "@example/mcp-server"]
```

**pi** ([pi-mcp-adapter](https://pi.dev/packages/pi-mcp-adapter)): pi has no
built-in MCP client; the adapter (`pi install npm:pi-mcp-adapter`) reads the
project's `.mcp.json`, so the Claude entry above serves pi too once the adapter is
installed.

**Antigravity** ([docs](https://antigravity.google/docs/mcp?tab=cli)): the
workspace-local `.agents/mcp_config.json` (or `~/.gemini/config/mcp_config.json`),
a `mcpServers` object of `command` / `args` / `env` in the same wrapped shape.

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
