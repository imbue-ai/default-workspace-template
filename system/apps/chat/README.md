# The chat app

The agent harness UI: live conversations with mngr-managed agents, one page per
chat, rendered inside a tab's iframe at the chat's own registered origin. It is
an app of the workspace like the terminal or the file viewer (the workspace app
model, `docs/system/blueprint/workspace-app-model/`): the shell knows it only
through its manifest (`app.toml`), its registry row, its instances API, and the
browser-side contract.

## What it serves

The `chat` program (declared in `system/supervisord.conf.d/chat.conf`) runs
`chat-app`, the console script of this package, from its own uv tool environment
(installed by `system/scripts/build_workspace.sh` with the mngr harness plugins
`system/config/mngr_plugins.toml` assigns to `chat`). At startup it registers
its manifest and port 8010 through `system/scripts/forward_port.py`, starts
`mngr observe` for the workspace's agents, and serves:

- `GET /<chat-id>` (and `/<chat-id>.<agent-id>.<session-id>` for a subagent view): the
  chat document, the built `chat.html` with the chat's ids, the workspace
  hostname, and the terminal app's origin label in meta tags.
- `/_instances`: the instances API of `contracts.md` section 4.3 over the agent
  manager (`instances.py`): every chat (a non-primary agent, today) is an
  explicit, renameable, stoppable instance keyed by its chat id (stop is `mngr
  stop`, start the same ensure-started path a send takes); a chat that is not an
  agent yet is a referenced provisional instance under the id mngr will give its
  first agent, whether it is waiting for an account (`attention`), being created
  (`working`), or failed (`error`); a subagent view is a referenced instance
  keyed `<chat-id>.<agent-id>.<session-id>`. A chat's status comes from its
  active agent's activity state, a pending permission request, and the
  lifecycle. The API answers `503` until the agent list has been read from mngr
  once.
- Every `/api/chats/<chat-id>/...` route (events, streams, sends, model choice,
  the queue actions, presence, destroy, start, stop; the subagent reads under
  `/api/chats/<chat-id>/agents/<agent-id>/subagents/<session-id>/`),
  `/api/chats/create`, `/api/chats`, `/api/harnesses`, `/api/uploads`,
  `/api/claude-auth`, `/api/accounts`, `/api/lanes`, and `/api/latchkey`. Every
  per-chat route, `/api/chats/create`, and the subagent reads are also served
  under their older `/api/agents/...` spelling, whose id is read as a chat id;
  `/api/agents` stays the plain listing of every mngr agent.
- `/api/ws`: the chat pages' socket, carrying `chats_updated` (a `ChatSnapshot`
  per chat, the agent-level facts under `active_agent`) and the provisional-chat
  events (`provisional_chat_created`, `provisional_chat_completed`).
- `/api/health`: `{"status", "is_frontend_built"}`, the probe the update apply
  polls on the `--preflight` boot (after the restart it polls `/_instances`, the
  route that answers only once the agent manager has its first list).
- Agent-authored files by their absolute on-disk path (`file_serving.py`), so a
  chat's markdown can show an image the agent wrote.

The chat page talks to the shell only through the browser-side contract
(`shell:open`, `shell:focused`, the handshake) and the shell reaches the chat
only over loopback (the instances API, the relay). Sends are reported to the
shell's client-activity route so agents can attribute a request to a client.

A chat is a sequence of agent transcripts run by one agent at a time
(`docs/system/blueprint/chat-agent-split/`); its id is its first agent's id, a
`ChatId` in code (`primitives.py`), and every agent this app creates carries it
as `MINDS_CHAT_ID` in its environment. A chat that has run on several agents
has a record under `data/.apps/chat/chats/<chat-id>/record.json`
(`chat_records.py`) naming its agents in order; every other agent is a chat of
its own. The agent manager resolves every chat through the records: the
instance list shows one chat per record, from its active agent, and never an
archived member; stop, start, rename, and status act on the active agent, and
destroy names every member. The read routes go through `chat_transcript.py`,
the chat's transcript as its agents' segments in order with an `agent_switch`
chip between them: an archived segment is read through its harness's
`TranscriptLoader` (the watcher without the watching), loaded on the first read
that reaches into it and dropped with the chat, and every event on the wire
carries its `agent_id`.

A handoff (`chat_handoffs.py`) is how a chat moves to another harness:
`POST /api/chats/<chat-id>/handoff` with an `account_id` and the message typed
for the new agent. The chat app stops the current agent's turn and hands its
queue back (draining), asks the agent for a summary through the
`handoff-summary` skill unless a fresh one exists (summarizing), then stops and
archives it under `archived-<seq>-<name>-<id>` with one `mngr rename`, records
its segment's length, and creates the successor under a pre-minted id with the
chat's name, the account's binding, `chat_id`/`chat_seq` labels, and a first
message filled in from `.agents/shared/references/continue-chat.md`
(switching). Every step is recorded on the chat record's `handoff` entry and
re-checked against mngr's state, so a restart of the app resumes an unfinished
handoff where it stopped. While a chat converges its instance stays listed as
`working` from the retiring agent; stop, start, rename, interrupt, the queue
actions, and the model change answer 409; a send is held (202, `{"status":
"held"}`) and delivered to the successor in order once it runs; destroy
proceeds. `POST .../handoff/cancel` calls it off before switching begins and
returns the confirming message for the composer; a failed create leaves the
chat in the `failed` phase with the reason, and `POST .../handoff/retry` with
an `account_id` runs the create again with the same prompt. The event fan-out
and the SSE streams are keyed by chat id, so an open page follows the chat
through the switch and sees the chip live. The summaries and prompts live
beside the record under `data/.apps/chat/chats/<chat-id>/`.

A rebind (`chat_rebinds.py`) is how a chat changes account on its own harness
and lane: the same route, dispatched on the target account's harness and lane
(the answer's `kind` says which it was). The chat app drains the agent's queue
as a handoff does, then stops the agent, repoints its binding in its own state
dir (the `CLAUDE_CONFIG_DIR` line of its env file for claude, after moving the
chat's session files into the new account's folder so `claude --resume` and the
watcher still find them; the credential symlink for codex, pi, and antigravity),
rewrites its `account` label, starts it again with `mngr start --no-resume`,
and delivers the held messages once it is up. The agent, its transcript, its tk
steps, and its model settings stay; the record's `rebind` entry carries the
state through a restart of the app, and a rebind on a one-agent chat drops the
record again when it completes. There is no cancel (the agent restarts as soon
as the switch is confirmed); a failed start leaves the chat in the `failed`
phase, and the retry offers the accounts of the same harness and lane.
`harnesses/binding.py`'s `REBIND_VERIFIED_HARNESSES` names the harnesses a chat
may be rebound on; a same-lane target on any other harness is a handoff.

The page drives both from the composer's provider menu: pressing any account
but the chat's own makes it the chat's pending lane ("next"), the send button
then reads "Switch and send" and asks a confirm (two lines for a handoff, one
for a rebind, with "Start a new chat instead" as the other way out), and the
typed message becomes the first the chat sends after the switch. While the chat converges the held messages
render from the snapshot's `handoff.held_sends` with the phase as their
caption, the activity strip and the placeholder say what is happening, and for
a handoff the Stop button is "Cancel switch" until the old agent is stopped; a
failed start shows its reason over the composer with a retry (on any signed-in
account after a handoff, on the same harness and lane after a rebind) and
"Start a new chat instead". The verbs the app refuses meanwhile answer 409 with
a detail written for the user, which the page and the shell's tab menu show as
is.

The send route is also how anything inside the workspace messages a chat:
`system/scripts/message_chat.py` posts to it by chat id (the browser app's
wake-ups, a lead's replies to a worker, the automation runner) and falls back
to `mngr message` only when the chat app cannot be reached or does not know the
chat. A send that names no client (no `client_id`, `device_kind`, or
`active_layout`) posts no client-activity report. The route answers 503 until
the agent list has been read from mngr once, like the instances API, so a send
during the app's first seconds is retried rather than mistaken for an unknown
chat. See `docs/system/blueprint/chat-agent-split/`.

## Provider accounts

Accounts live under `~/.minds/accounts` (`accounts.py`): one folder per
signed-in provider account plus an index, minted by the sign-in flows
(`harnesses/auth_flows.py`) the chat page's provider chooser drives. A chat
binds to an account when it is created and moves to another only through a
switch (a handoff or a rebind, above). A launch that names no account (the New
Tab tile, a rail shortcut, `layout.py open chat`) goes to the account the user
pinned as the default in a chat's provider menu, else to the most recently used
one; pressing another account in that menu makes it the chat's pending lane. `system/scripts/migrate_claude_auth.py` imports this package from
the root venv.

The same default reaches every `mngr create` in the workspace that names no
harness and no account -- the chats the Mind app starts from outside, workers,
automations, the caretaker -- through `.mngr/settings.local.toml`, mngr's
git-ignored local config layer (`create_defaults.py`). The account store writes
it on every index write and at boot: `[commands.create]` with the default
account's harness as `type`, its binding (`env__extend` for claude, an
`extra_provision_command__extend` credential link over `$MNGR_AGENT_STATE_DIR`
for codex, agy and pi) and the `account=<id>` label a re-auth restarts agents
by. The pin and the most recently used account stay in `index.json`; the file is
derived from them and nobody is expected to edit it, though keys outside the
managed ones survive every rewrite. With no usable account the managed keys are
removed, and a create in the workspace is then refused by
`system/scripts/require_create_account.py` (mngr's `pre_command_scripts.create`
entry in `.mngr/settings.toml`) with a message that says to sign in.

A chat created from outside the workspace with an `auto_open` or `assist` label
(the Mind app's update and help chats) has its tab surfaced by this app
(`auto_open.py`): when the agent appears, the app asks the shell to open the
chat's address in every connected client, holds the open until a client is
connected if none is, and records the delivery under
`data/.apps/chat/auto_opened_chats.json` so a restart never re-pops a tab. The
open is held for as long as the chat exists, so a chat started while nobody was
connected still gets its tab whenever someone finally connects. The one
exception is a workspace with no ledger to read (its chats predate this app
keeping one, or the file was lost): every labeled chat it already has is adopted
as shown, since a tab for each is worse than missing one.

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

`--preflight` is the update apply's throwaway boot (`.agents/skills/update-self`):
the app imports, builds, and serves `/api/health` but reconciles no accounts (the
boot sweep reaps sign-in processes), starts no agent manager (so no `mngr observe`,
session sweep, memory prioritizer, or nudges to the shell), and registers nothing.
The apply boots the merged chat this way on a free port before restarting the live
services, since this is the process that imports mngr and the harness plugins, and
refuses the update when it cannot come up.

The frontend lives in `frontend/` and builds into `imbue/chat/static/`; see
`system/apps/README.md` for the shared frontend library and the npm
workspace both frontends belong to.

## Memory shedding

The chat app re-tags chat agents' `oom_score_adj` from live activity
(`oom_prioritizer.py`), fed by the pages' presence reports and by its own send
path, and keeps each chat's last-messaged stamp under `data/.apps/chat/` so a
restart seeds the ranking from real history. The app itself runs in the `chat`
band, just above the shell.
