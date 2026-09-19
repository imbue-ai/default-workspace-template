---
name: connect-external-service
description: Connect to, integrate with, read from, or act on any external service, account, website, or files outside this workspace, whether or not you think a connection already exists. Covers latchkey (the user's connected accounts), a new connection to a service latchkey does not know, the service's MCP server, its SDK or API with a key the user supplies through a secret card, and driving a browser. Use before touching anything outside the workspace on the user's behalf.
compatibility: Requires node.js, curl and latchkey (npm install -g latchkey).
metadata:
  author: imbue
---

# Connecting to an external service

Every way of reaching something outside this workspace on the user's behalf, in
the order to try them. Take the **first workable row** and say in one or two
sentences which you chose and why. The user may be non-technical: decide, then
explain; do not ask them to choose a mechanism.

## The routing table

| # | Method | Workable when | What you say | Read |
|---|---|---|---|---|
| 1 | **Builtin latchkey service** | `latchkey services list` names the service (`--viable` shows the ones already connected; `latchkey services info <name>` gives auth options and status). | "I'll use your connected Slack account." | `references/latchkey.md` |
| 2 | **Custom latchkey service** | The API is HTTPS and takes a header credential (`Authorization: Bearer <token>` or a named header via `payload.header`), or the site has an internal API behind a cookie or minted token you can identify (`cookie-capture`, `token-capture`). A key that goes in the query string is NOT workable here; go to row 4. | "I'll ask you to approve a connection to api.example.com and paste its API key into the approval window; the key stays in Mind's credential store." | `references/latchkey.md`, "Custom services" |
| 3 | **MCP server** | A maintained MCP server exists for the service. Prefer one authenticated by a key, or one running locally over stdio, to one with its own OAuth sign-in. | "Example ships an MCP server, so I'll wire that up; it needs an API key, which I'll ask you for." | `references/mcp.md` |
| 4 | **Direct API** | An official SDK, CLI, or documented HTTP API takes a key the user can copy from their account settings. An API that needs an OAuth app registration is workable too, but offered as the more technical option, with row 5 the default. | "Example has an API; I'll ask you for its key and call it from here." | `references/direct-api.md` |
| 5 | **Browser** | Always. Sign-ins, CAPTCHAs, and two-factor prompts go to the user through `handoff`. | "I'll drive a browser you can watch and take over." | the `agentic-browser-fleet` skill |

A row's test is cheap on purpose: run `latchkey services list --viable` first,
then look at the service's docs for the shape its API takes. Do not run the
whole table when an earlier row is plainly workable.

## Rules that apply to every row

- **Decide, then explain.** Pick the first workable row and tell the user which
  and why in plain terms. At the one fork that is genuinely their preference
  (registering an OAuth app for row 4 versus driving the browser for row 5),
  default to the browser and mention the OAuth alternative in one line.
- **A credential value never enters a command, a file you write, or your prose.**
  Rows 1 and 2 keep the value in Mind's credential store and inject it at the
  gateway. Rows 3 and 4 take it through the **secret card**: you run
  `request_secret.py` (below), the user types the value into the card, and the
  chat app writes `data/.secrets/<name>.env`. You never see the value; you name
  the file and the variables. A program reads the file only through
  `python3 system/scripts/with_secrets.py data/.secrets/<name>.env -- <command...>`,
  which puts the variables in that process's environment. A guard refuses every
  other read (`cat`, `source`, `Read`, ...). To rotate a value, request it again;
  to remove one, `rm` the file.
- **A secret request stands alone in its tool call**, output untouched, and you
  end the turn after it prints: the chat builds the card from the echoed JSON
  and the answer arrives as a message (`Secret stored: ...` or `Secret declined:
  ...`). The same rule as a latchkey permission request, enforced by the same
  guard. Do not ask the user to tell you when they have answered.
  ```bash
  python3 .agents/skills/connect-external-service/scripts/request_secret.py \
    --file example --var EXAMPLE_API_KEY --rationale "I need your Example API key to look up the part numbers you asked about."
  ```
- **A background agent does not request a secret.** A worker (`launch-task`)
  has no chat to show a card in; it reports the need in its finish report and the
  lead requests it.
- **No chat app, no card.** `request_secret.py` fails plainly when it cannot
  reach the chat app (an agent created outside Mind, a headless run). The last
  resort is to ask the user to place `data/.secrets/<name>.env` from a terminal
  themselves, with one `NAME='value'` line per variable and `chmod 600`.
- **Look for the newest documentation of the public API online**, whichever row
  you land on, and prefer official endpoints to bot-only ones.
- **Never discuss the mechanism unless asked.** "Your connected Slack account",
  "a key you paste into the card", "a browser you can watch" are the user's
  terms; latchkey, MCP, gateways, and env files are not.
