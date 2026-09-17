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
  `/api/claude-auth`, `/api/accounts`, `/api/lanes`, and `/api/latchkey`.
  `/api/agents` is the plain listing of every mngr agent (the loopback callers'
  view of background agents too); the older `/api/agents/<id>/...` spellings of
  the per-chat routes are gone.
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
marker between them: an archived segment is read through its harness's
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
chat's name, the account's binding and `chat_id`/`chat_seq` labels, carrying no
message of its own (switching); the prompt filled in from
`.agents/shared/references/continue-chat.md` follows through the send path. The
prompt carries the summary in full when it is 64 KB or under
(`INLINE_SUMMARY_MAX_BYTES`) and otherwise only its path, which the successor is
told to read before anything else. Every step is recorded on the chat record's `handoff` entry and
re-checked against mngr's state, so a restart of the app resumes an unfinished
handoff where it stopped. While a chat converges its instance stays listed as
`working` from the retiring agent; stop, start, rename, interrupt, the queue
actions, and the model change answer 409; a send is held (202, `{"status":
"held"}`) and delivered to the successor in order once it runs; destroy
proceeds. `POST .../handoff/cancel` calls it off before switching begins and
returns the confirming message for the composer; a step that fails past the
point of no return -- the create, the model pick, a delivery, or one mngr
refuses -- leaves the chat in the `failed` phase with the reason and the step
(`failed_step`), and `POST .../handoff/retry` with an `account_id` runs that
step again, keeping the pre-minted id so a successor an earlier attempt made is
adopted rather than made twice. A retry may name another account until the
successor is the chat's own agent; after that only the account it moved to. The
event fan-out and the SSE streams are keyed by chat id, so an open page follows
the chat through the switch and sees the node live. The summaries and prompts live
beside the record under `data/.apps/chat/chats/<chat-id>/`.

A rebind (`chat_rebinds.py`) is how a chat changes account on its own harness
and lane: the same route, dispatched on the target account's harness and lane
(the answer's `kind` says which it was). The chat app drains the agent's queue
as a handoff does, then stops the agent, repoints its binding in its own state
dir (the harness's account binding, `harnesses/<harness>/account_binding.py`:
the `CLAUDE_CONFIG_DIR` line of its env file for claude, after moving the chat's
session files into the new account's folder so `claude --resume` and the
watcher still find them; the credential symlink for codex, pi, and antigravity),
rewrites its `account` label, starts it again with `mngr start --no-resume`,
applies the model picked for it if one was, and delivers the held messages once
it is up. The agent, its transcript, its tk steps, and, unless another model was
picked, its model settings stay; the record's `rebind` entry carries the state
through a restart of the app, and a rebind on a one-agent chat drops the record
again when it completes. There is no cancel (the agent restarts as soon as the
armed switch's next message carries it out); a failed start or a model pick the
agent cannot take leaves the chat in the `failed` phase, and the retry offers
the accounts of the same harness and lane.
`harnesses/binding.py`'s `REBIND_VERIFIED_HARNESSES` names the harnesses a chat
may be rebound on; a same-lane target on any other harness is a handoff.

The page drives both from the composer's provider menu: pressing an account on
another harness opens the switch dialog ("Switch to Codex?"), which takes the
model the successor runs on. "Switch this chat" arms the switch: a strip above
the composer says what the next message does, the model bar reads as the
target, and the send button reads "Switch and send" and carries the switch out
with the typed message as the first the chat sends after it, with no second
confirmation. "Start a new chat" opens a chat on that account and model
instead, with the draft moved over. Pressing an account on the chat's own
harness and lane (a rebind) asks nothing: the agent keeps its conversation and,
by default, its model, so the press arms the switch at once and the next
message carries it out. The strip's Change (and the model bar's Model row)
opens the dialog's rebind variant, whose picker starts from "Keep the current
model", for changing account and model in one switch. A chat that has had no
user turn skips the dialog too: it switches at once, with no summary and no
handoff prompt, since there is nothing to hand over. Only a switch that will
write a summary asks.
While the chat converges the held messages render from the snapshot's
`handoff.held_sends` (the message the user switched with stands down once the
`agent_switch` marker carrying it is on the transcript, where it renders as the
successor's opening bubble), one handoff node in the transcript shows the switch's
progress ("Handing off to Codex...", then "Handed off from Claude Code to Codex",
expandable to the summary turn and the handoff prompt, with a rule under it
once the switch has landed; a fresh start, which asked for no summary and sent
no prompt, leaves no node once it has landed), the activity strip and the placeholder say
what is happening, and for a handoff the Stop button is "Cancel switch" until
the old agent is stopped; a failed start or a model pick the successor cannot
take shows its reason over the composer with a retry (on any signed-in account
after a handoff, on the same harness and lane after a rebind) and "Start a new
chat instead". The verbs the app refuses meanwhile answer 409 with a detail
written for the user, which the page and the shell's tab menu show as is.

A handoff's successor is created silent: its model pick is applied first
(`POST /api/chats/<chat-id>/handoff` takes `model`), then the handoff prompt
goes to it through the send path, then the held messages. A rebind's pick is
applied the same way once the agent is back on the new account, retried for a
while since `mngr start` does not wait for the harness to come up. A new chat
created with a pick (`POST /api/chats/create` takes `model` too) is set up the
same way. `GET /api/accounts/<account-id>/model-options` is where the dialog
gets the target account's models, for a handoff's successor and a rebound agent
alike: the catalog for a static harness, for codex the options an agent of the
account was last offered, and nothing for antigravity, whose model is changed
from the agent's terminal.

The send route is also how anything inside the workspace messages a chat:
`system/scripts/message_chat.py` posts to it by chat id (the browser app's
wake-ups, a lead's replies to a worker, the automation runner) and falls back
to `mngr message` only when the chat app cannot be reached or does not know the
chat. A send that names no client (no `client_id`, `device_kind`, or
`active_layout`) posts no client-activity report. The route answers 503 until
the agent list has been read from mngr once, like the instances API, so a send
during the app's first seconds is retried rather than mistaken for an unknown
chat. See `docs/system/blueprint/chat-agent-split/`.

A chat can also start from a conversation that happened before the workspace
existed. `POST /api/chats/seed` (`chat_seed.py`; the Mind app runs
`system/scripts/seed_welcome_chat.py` through `mngr exec` the moment a
workspace is ready) takes a title and the turns of the onboarding conversation
and opens a chat on them: the turns are written as the chat's first segment
(`data/.apps/chat/chats/<chat-id>/seed.jsonl`, read through the `seed`
pseudo-harness like any archived segment), the record names the seed as its
first member, and the chat is listed as a provisional chat in the
`awaiting_first_send` phase, its transcript on the page with a composer under
it. The user's first message is what launches the chat's first real agent
(the provider chooser opens then if nothing is signed in), which joins the
record as the seed's successor with the `chat_id` and `chat_seq` labels a
handoff's successor carries. The seed survives a restart of this app because
the record does; discarding the chat before its first send drops both.

Every chat that starts with no message is greeted: the `welcome` create
template (`.mngr/settings.toml`) sends `/welcome`, and the skill varies what it
says by how many times it has run (`system/scripts/welcome_count.py`). Fast mode
is a per-chat setting with three modes (`chat_fast_mode.py`, kept in the chat's
folder as `fast_mode.json`, `GET`/`PUT /api/chats/<chat-id>/fast-mode`):
**off** (standard speed throughout), **auto** (fast for the first
`fast_mode_turn_limit` of the user's turns, then standard speed) and **on**
(fast throughout). A new chat starts in the workspace's default mode
(`fast_mode_default` in `GET`/`PUT /api/settings`, `chat_settings.py`, stored
at `data/.apps/chat/settings.json`; auto with a limit of 5 unless changed), and
a chat whose mode calls for it launches through the `fast` create template, a
handoff's successor included. The model picker's fast row states the chat's
mode and opens a small chooser where the mode, auto's turn limit and the
default for new chats are set; `/fast on` and `/fast off` typed in the
composer choose the mode too. The first time auto switches a chat in a
workspace, a one-time notice over the model bar explains it.

## Provider accounts

Accounts live under `~/.minds/accounts` (`accounts.py`): one folder per
signed-in provider account plus an index, minted by the sign-in flows
(`harnesses/auth_flows.py`) the chat page's provider chooser drives. A chat
binds to an account when it is created and moves to another only through a
switch (a handoff or a rebind, above). A launch that names no account (the New
Tab tile, a rail shortcut, `layout.py open chat`) goes to the account the user
pinned as the default in a chat's provider menu, else to the most recently used
one; pressing another account in that menu switches the chat to it (through
the dialog, or at once for a chat with nothing to hand over -- see the switch
above). `system/scripts/migrate_claude_auth.py` imports this package from
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
