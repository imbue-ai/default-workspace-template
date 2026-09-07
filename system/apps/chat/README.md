# The chat app

The agent harness UI: live conversations with mngr-managed agents, one page per
chat, rendered inside a tab's iframe at the chat's own registered origin. It is
an app of the workspace like the terminal or the file viewer (the workspace app
model, `docs/system/blueprint/workspace-app-model/`): the shell knows it only
through its manifest (`app.toml`), its registry row, its instances API, and the
browser-side contract.

## What it serves

The `chat` program (`system/supervisord.conf`) runs `chat-app`, the console
script of this package, from its own uv tool environment (installed by
`system/scripts/build_workspace.sh` with the mngr harness plugins
`system/config/mngr_plugins.toml` assigns to `chat`). At startup it registers
its manifest and port 8010 through `system/scripts/forward_port.py`, starts
`mngr observe` for the workspace's agents, and serves:

- `GET /<agent-id>` (and `/<agent-id>.<session-id>` for a subagent view): the
  chat document, the built `chat.html` with the chat's ids, the workspace
  hostname, and the terminal app's origin label in meta tags.
- `/_instances`: the instances API of `contracts.md` section 4.3 over the agent
  manager (`instances.py`): every non-primary agent is an explicit, renameable,
  stoppable instance keyed by its agent id (stop is `mngr stop`, start the same
  ensure-started path a send takes); a chat still being created is a referenced
  provisional instance under the id mngr will give it; a subagent view is a
  referenced instance keyed `<agent-id>.<session-id>`. Status comes from the
  activity state, a pending permission request, and the lifecycle. The API
  answers `503` until the agent list has been read from mngr once.
- Every `/api/agents/...` route (events, streams, sends, model choice, the
  queue actions, presence, destroy, start, stop), `/api/agents/create-chat`,
  `/api/harnesses`, `/api/uploads`, `/api/claude-auth`, `/api/accounts`,
  `/api/lanes`, and `/api/latchkey`, verbatim as the shell served them before
  phase 10.
- `/api/ws`: the chat pages' socket, carrying `agents_updated` and the
  proto-agent events.
- `/api/health`: `{"status", "is_frontend_built"}`, the probe the update apply
  polls after a restart.
- Agent-authored files by their absolute on-disk path (`file_serving.py`), so a
  chat's markdown can show an image the agent wrote.

The chat page talks to the shell only through the browser-side contract
(`shell:open`, `shell:focused`, the handshake) and the shell reaches the chat
only over loopback (the instances API, the relay). Sends are reported to the
shell's client-activity route so agents can attribute a request to a client.

## Provider accounts

Accounts live under `~/.minds/accounts` (`accounts.py`): one folder per
signed-in provider account plus an index, minted by the sign-in flows
(`harnesses/auth_flows.py`) the chat page's provider chooser drives. A chat
binds to an account when it is created and never changes it. A launch that names
no account (the New Tab tile, a rail shortcut, `layout.py open chat`) goes to the
account the user pinned as the default in a chat's provider menu, else to the
most recently used one; pressing another account in that menu offers to launch a
new chat on it. `system/scripts/default_account_args.py`
and `system/scripts/migrate_claude_auth.py` import this package from the root
venv.

## Development

```bash
# Backend, from the repo root (the app's data paths are relative to it)
uv run chat-app --no-register

# Tests
cd system/apps/chat
uv run pytest
```

`--no-register` boots the app without re-pointing the live chat row in the
registry, for a throwaway boot on another port (`CHAT_PORT`).

The frontend lives in `frontend/` and builds into `imbue/chat/static/`; see
`system/apps/README.md` for the shared frontend library and the npm
workspace both frontends belong to.

## Memory shedding

The chat app re-tags chat agents' `oom_score_adj` from live activity
(`oom_prioritizer.py`), fed by the pages' presence reports and by its own send
path, and keeps each chat's last-messaged stamp under `data/.apps/chat/` so a
restart seeds the ranking from real history. The app itself runs in the `chat`
band, just above the shell.
