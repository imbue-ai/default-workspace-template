# Plan: chat-agent-split -- a chat is a sequence of agents

## Overview

- Today a chat and an mngr agent are the same object: the chat's instance key, page URL, WebSocket state, API routes, and every frontend identifier are the agent id, and the harness and account are fixed at `mngr create`.
  Changing the lane (the provider plus harness pairing the user signs in to) therefore forces a new chat.
- This plan separates the two.
  A *chat* is the user-facing conversation: one tab, one instance key, one continuous transcript.
  An *agent* is one mngr agent running one harness on one account.
  A chat is an ordered sequence of one or more agents, exactly one of which is *active* at a time; every earlier one is *archived*.
  A lane change in an existing chat becomes a *handoff*: the active agent finishes what it is doing, writes a summary, stops, is archived, and a new agent on the new lane continues the same chat with that summary.
  An account change on the same harness is a *rebind*: the same agent is stopped, repointed at the new credential, and restarted in place.
- The chat id is the id of the chat's first agent.
  Every existing chat, URL, layout file, instance key, and external caller therefore stays valid with no migration, and an agent that no multi-agent chat record contains is its own chat.
- The frontend only ever speaks about chats.
  The chat app's backend owns the chat-to-agents mapping and multiplexes every message, read, and verb to the right agent.
  The shell, the minds desktop app, and every skill address a chat by its chat id, never by an agent's mngr name or id.
- The work lands as seven phases, each a separately merged pull request on this repository, with the first two behavior-neutral: first, every in-workspace sender messages a chat through the chat app rather than through `mngr message`; second, the pure rename that fixes every chat-versus-agent name without changing behavior.
  Phases three through seven add the multi-agent model, the handoff, its UI, the rebind, and the cleanup.
- This document is the reference every phase is judged against.
  It records each decision together with the rationale given for it, and errs on the side of detail.

## Related documents

- `docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md` and its `contracts.md`: the app model this plan builds on; section 4.3 of the contracts (the chat row) and section 7.4 of the meta spec are the parts this plan revises.
- `system/apps/chat/imbue/chat/harnesses/core-contracts/messages-lifecycle-contract.md`: the conservation contract every handoff step must satisfy.
- `docs/system/blueprint/simplify-chat-data-model/plan-simplify-chat-data-model.md`: the transcript store and watcher eviction the multi-segment transcript reuses.
- `system/apps/chat/README.md`: the chat app as it stands before this plan.
- The minds repo's `apps/minds/docs/workspace/glossary.md`, whose "chat agent" entry ("one per chat tab") this plan retires.

## 1. Vocabulary

Code and documentation use these names.

| Term | Meaning |
|---|---|
| Chat | The user-facing conversation: one tab, one instance key, one continuous transcript. Identified by its chat id. |
| Chat id | The id of the chat's first agent, an mngr agent id (`agent-<32hex>`). A distinct type (`ChatId`) from `AgentId` in code, even though the two strings are equal for a chat's first agent. |
| Agent | One mngr agent: one harness process, bound to one account, with its own transcript files, state dir, and mngr name. |
| Segment | The part of a chat's transcript one agent produced. A chat's transcript is its segments in agent order. |
| Active agent | The one agent of a chat that receives messages and whose lifecycle is the chat's status. Every chat has exactly one, except transiently during a handoff. |
| Archived agent | An earlier agent of a chat: stopped, renamed to its archival name, labeled `archived_at`, never messaged again, kept for its transcript. |
| Chat record | The document under `data/.apps/chat/chats/` that lists a multi-agent chat's agents in order and carries its handoff state. Exists only for chats that have had a handoff. |
| Own-chat rule | An agent that no chat record contains is a chat by itself, with the chat id equal to its agent id. |
| Lane | A (provider, harness) pairing a user signs in to: `harnesses/lanes.py`. Several lanes can share a harness. |
| Harness | The agent program a lane runs on (`HarnessType`: claude, codex, pi-coding, opencode, antigravity). |
| Account | One signed-in provider account under `~/.minds/accounts` (`accounts.py`); an account is on exactly one lane. |
| Handoff | Continuing a chat on a different harness: the active agent converges, is archived, and a new agent takes over with a summary. |
| Rebind | Continuing a chat on a different account of the same harness: the same agent is stopped, repointed, and restarted in place. |
| Converging | The handoff's transitional condition, from the user's confirmation to the new agent being active. Its phases are listed in section 5.4. |
| Summary | The markdown document the retiring agent writes for its successor, at a path the chat app names. |
| Pending lane | The lane the user has chosen in the UI but not yet applied. Frontend-only state until the next send. |
| `MINDS_CHAT_ID` | The environment variable every chat-app-created agent carries, naming the chat it belongs to. |

Retired vocabulary: "chat agent" as a synonym for a chat, "proto agent" (now "provisional chat"), and addressing a chat by its mngr agent name.

## 2. Motivation

### 2.1 What the user wants

A chat should be an indefinitely long sequence of messages, exactly as it is today from the frontend's point of view.
Switching lane should be something the user does inside that chat, not a reason to open another one.
On the backend the chat app must know exactly which agent each message went to and each transcript event came from, because the agents differ in harness, account, transcript format, and mngr identity.

For speed, a new chat still starts its first agent immediately on the default lane, exactly as today.
Nothing in this plan changes when the first agent starts.

### 2.2 Why the two concepts are tangled today

- The instance key is the agent id, the page is `/<agent-id>`, and the shell address is `app:chat?instance=<agent-id>` (contracts section 4.3).
  Subagent views are keyed `<agent-id>.<session-id>`.
- The chat WebSocket pushes `agents_updated`, `proto_agent_created`, and `proto_agent_completed`, and every route is `/api/agents/<agent_id>/...`.
- `AgentStateItem`, `AgentInfo`, `ProvisionalChat.agent_id`, `CreatedChatAgent`, and every `*_by_agent` dict in `AgentManager` key on the agent id, and `AgentManager` mixes chat-level duties (naming, the first-chat claim, provisional records, the project label, nudging the shell) with agent-level ones (the observe stream, mngr create, destroy, stop, and rename, trackers, sessions, model watchers).
- The lane, account, and harness are fixed at `mngr create` (`harnesses/binding.py`), so "switch provider" in the model bar can only open a sibling chat (`startChatOnAccount`).
- The display name lives on the agent as a `display_name` label with its canonical form as the mngr name, and auto-names are per harness or lane word ("Chat 1", "Codex 2", "Pi 3").
- The frontend uses `agentId` about four hundred times, keys its stores by it, and reads agent-level facts directly: the harness for the catalog lookup, the mngr name for the terminal back face, the model choice.
- One watcher tails one agent's harness files, and paging offsets and totals are per agent.
- Outside the chat app, the minds e2e runner finds the chat frame by the `/agent-<hex>/` URL, the minds_evals bridge calls `/api/agents/create-chat` and `/api/agents/<id>/message` and `/events`, the `automation` create template bakes `app:chat?instance=$MNGR_AGENT_ID` into its system prompt, and several skills speak agent ids.
- Workers reach their lead by mngr name: `create_worker.py` stamps `lead_agent` from `MNGR_AGENT_NAME`, the worker pushes its report with `mngr rsync "$LEAD_AGENT:..."` and reads the lead's transcript with `mngr transcript $LEAD_AGENT`, and `tk` stamps step records with `MNGR_AGENT_NAME`.
  A worker never messages its lead: the lead polls for the report file (`create_worker.py await`).
- The in-workspace senders that do message a chat all use `mngr message`: the browser app's wake of the agent that owns a browser (`session.py`, `_message_agent`), the lead's replies to a worker's gate (`lead-proxy.md`, dead-worker-recovery, and the update-self, migrate-workspace, and fetch-process-show skills), and the automation runner's `/clear` and `/<skill>` sends (`run_automation.sh`).

### 2.3 What is already broken that this plan fixes on the way

Anything that addresses a chat by its mngr agent name is wrong today, not only after this plan: a user rename changes the mngr name mid-task, so a worker that captured `LEAD_AGENT` at launch pushes its report to a name that no longer exists (the same-repo fallback write in `worker-reporting.md` is what saves the report) and its `mngr transcript $LEAD_AGENT` fails outright.
Section 4.5 moves every in-workspace sender to chat-id addressing through the chat app, and every mngr-targeted reference to the lead to its id.

## 3. Principles and settled decisions

Each decision below was settled during design, with the rationale given at the time.

1. **The chat id is the first agent's id.**
   It is backwards compatible with every stored URL, layout, instance key, and external caller, it gives agents created outside the chat app a sensible identity, and it is why a chat has one or more agents rather than zero or more.
2. **The chat record lives in a chat-app store**, under `data/.apps/chat/`, and is written only for chats that have had a handoff.
   A chat whose harness was never switched needs no record.
3. **An agent is its own chat if no chat record contains it** (the own-chat rule).
4. **The frontend speaks only about chats**, and receives one `active_agent` object per chat for the few harness-specific things it renders.
   Subagent instance keys include the agent the session came from.
5. **A chat has one watcher at a time**: the previous agent is shut down before the next one starts, so the live watcher is always the active agent's.
6. **The title lives on the active agent** (its `display_name` label, with the canonical form as its mngr name), and the new agent reuses the retiring agent's mngr name.
7. **Existing routes stay as aliases** while every other surface moves to chat-keyed routes; the aliases are dropped in the last phase.
8. **A handoff converges the old agent first**: finish or cancel message delivery, stop it, produce a summary, and only then switch.
   Summarize-first ordering is required because a summary may already exist (a future cache-TTL summarizer will write one automatically), in which case the summary step is skipped for free.
9. **A chat's status is its active agent's status**, plus the converging condition during a handoff.
   Stop and start act on the active agent.
   Destroying a chat destroys every one of its agents; archived agents are otherwise never touched.
10. **The summary is a file the retiring agent writes** at a path the chat app names, requested by a message.
    A future mngr plugin may produce it per harness; for now the request is a slash command backed by a skill in this template.
    The chat app must be robust to the agent failing to write it, including failing immediately.
11. **The lane change applies on the next send**, never while a turn is in flight on its own.
    The user can change the pending lane freely; the next send asks for confirmation.
12. **tk step records are closed at handoff** and their titles handed to the new agent, which decides its own steps.
13. **Rebind and handoff are different operations** behind one user-facing gesture ("switch provider").
    Rebind keeps the agent, its transcript, its tk steps, and its model settings; a handoff replaces the agent.
    Where a harness cannot resume a session under a swapped credential, a rebind falls back to a handoff.
14. **Summarize-first, and reuse an existing fresh summary.**
15. **Name-based addressing of chats is retired.**
    In-workspace senders message a chat through the chat app by chat id, with `mngr message` only as a backoff when the chat app cannot be reached.
    The rule has no exceptions: a worker and an automation agent are chats under the own-chat rule, so a lead's reply to a worker goes through the chat app too.
    Worker skills need no instructions about the handoff window because the chat app holds sends during it.
    Where mngr itself is the target (`mngr rsync`, `mngr transcript`), the lead is named by its agent id, which a rename does not change.
16. **The new agent receives as much context as could be relevant**, through `mngr create --message`: the summary path or the note that none exists, the archived agent's identity and transcript locations, the closed steps, and the user's message.
17. **The archival name sorts and reads correctly**: `archived-<seq>-<canonical>-<agent-id>`, and the archived agent carries the `archived_at` label (`mngr archive`).
18. **A failed create after the old agent was archived leaves the chat in a failed-next-agent state** with retry on any lane, and every retry passes the same summary message.
19. **Cancel is possible until the old agent is stopped.**
    Confirmation is not the point of no return: cancelling while the summary is being written leaves nothing to undo, because the queued messages are already back in the composer and the summary turn finishes harmlessly.
20. **The whole transition is durable, idempotent, and resumable**: a restart of the chat app at any point resumes from where it left off by reconciling against reality, never by replaying.
21. **Stop, start, and rename of a chat are refused while it is converging**; destroy is allowed as an abort.
22. **An agent whose first message carries no summary pointer, and that is not the first agent of its chat, gathers its own context** from its predecessors' transcripts.
    `AGENTS.md` tells every agent so.
23. **Every id that leaves the workspace to identify a chat is the chat id.**
    Agent ids remain only where mngr itself is the target.
24. **Model, effort, and fast mode reset on a harness handoff** to the new harness's defaults; a rebind keeps them.
25. **A stopped active agent is started to write its summary** rather than skipped: an agent with its full context writes a better summary than a successor reconstructing one, and skipping is only the backoff when the agent cannot be started at all.
26. **Testing uses several real accounts**; there is no same-lane "fresh agent" action for CI's sake.
27. **No mngr code change is required.**
    The chat app edits an agent's env file directly for a rebind, which is what the file exists for.
    The one mngr change the arc depends on landed separately before it started: mngr PR 908 made a local `[commands.create]` add to the project's defaults instead of replacing them, which is what lets `.mngr/settings.local.toml` carry the default account (section 4.5).

## 4. The model

### 4.1 Identity

A `ChatId` is a distinct type from mngr's `AgentId` so the type checker finds every crossing between the two.
Its string value is the chat's first agent's id, so it keeps the `agent-<32hex>` shape; a chat page URL is `/<chat-id>`, the instance key is the chat id, and the shell address is `app:chat?instance=<chat-id>`, all byte-identical to today for every existing chat.
The minds e2e runner's chat-frame pattern (`/agent-[0-9a-f]+/`) therefore stays valid.
A provisional chat (one minted before its agent exists) is unchanged: the id the chat app mints for the future first agent is the chat id, so the tab never changes address.

A subagent view is keyed `<chat_id>.<agent_id>.<session_id>`.
The middle component names the agent whose harness session the subagent belongs to, so a subagent of an archived agent still resolves to the right transcript files.
The longest such key is 38 + 1 + 38 + 1 + 36 characters, under the instance-key limit of 128 (contracts section 1).
Subagent records are `referenced` and held in memory, so the key change needs no migration; a stale layout entry is pruned by the shell as any missing instance is.

### 4.2 The chat record

Path: `data/.apps/chat/chats/<chat_id>/record.json`, beside `data/.apps/chat/chats/<chat_id>/summaries/`.
Chat-owned state lives under `data/.apps/chat/` (contracts section 17), and the message-stamps store already lives there.

Written atomically (temp file plus rename) under a per-chat lock, the same discipline as the accounts index.
The store is a `MutableModel` implementation behind a small interface so tests use an in-memory one.

Contents:

```json
{
  "version": 1,
  "chat_id": "agent-<hex of the first agent>",
  "agents": [
    {"seq": 1, "agent_id": "agent-...", "lane": "anthropic", "account_id": "...", "harness": "claude",
     "started_at": "...", "ended_at": "...", "archived_name": "archived-1-Chat-2-agent-...",
     "final_event_count": 812},
    {"seq": 2, "agent_id": "agent-...", "lane": "openai", "account_id": "...", "harness": "codex",
     "started_at": "...", "ended_at": null, "archived_name": null, "final_event_count": null}
  ],
  "handoff": null
}
```

- `agents` is in order; the last entry is the active agent unless `handoff` is set.
- `final_event_count` is recorded when an agent is archived, so chat-global offsets and totals need no loading of archived segments (section 4.7).
- `handoff` holds the in-progress handoff (its shape follows this list) and is `null` otherwise.
- The record is authoritative for order, membership, and handoff state.
  Every member agent from the second onward also carries the mngr labels `chat_id=<chat id>` and `chat_seq=<n>`, and the first agent gets `chat_id` and `chat_seq=1` through `mngr label` at its first handoff (`mngr label` works on stopped agents too).
  The labels let `mngr list --include 'labels.chat_id == "..."'`, agent-side scripts, and the find-transcripts skill find a chat's members without asking the chat app; the record remains the truth where the two disagree.
- A record is created at the first handoff, never at chat creation.
  A record whose `version` is newer than the running code refuses to load, matching the accounts index.

The `handoff` entry:

```json
{
  "phase": "summarizing",
  "started_at": "...",
  "target_lane": "openai",
  "target_account_id": "...",
  "retiring_seq": 1,
  "next_agent_id": "agent-<pre-minted>",
  "next_seq": 2,
  "summary_outcome": null,
  "closed_step_titles": [],
  "prompt": null,
  "trigger_message": "the user's message",
  "held_sends": [{"message_id": "...", "text": "...", "origin": "script"}],
  "error": null
}
```

`summary_outcome` becomes `reused`, `written`, or `missing` once summarizing ends; `prompt` is filled when switching begins (5.8); `error` is set in the failed phase (5.10).

The own-chat rule: when listing instances, every non-primary agent that appears in no record as a non-first member is a chat of its own.
Archived agents are excluded from the instance list because the record names them, not because of the `archived_at` label.
A bare `mngr list` does not hide `archived_at` agents (only `--active` excludes them), so the chat app must never rely on mngr's listing to filter them.

### 4.3 The active agent and its name

The chat's mngr name is the canonical form of its title ("Chat 2" is the `display_name` label and `Chat-2` the mngr name), exactly as today.
Every agent that becomes active takes that name, which keeps the terminal back face, the git commit identity (`agent_rewrite_bash_command.py` reads the live name), and `mngr list` readable.
Name reuse is not load-bearing for correctness: nothing may depend on reaching the chat by that name (section 4.5).

The retiring agent is renamed to `archived-<seq>-<canonical>-<agent-id>` with `mngr rename`, which also moves its tmux session, and given `display_name="<title> (archived <seq>)"` through the rename's `-l` flag, and then archived with `mngr archive`, which sets `archived_at`.
The archival prefix sorts archived agents together and the sequence number orders them; the agent id keeps the name unique for the life of the host.
`AgentName` allows letters, digits, dashes, and underscores with no length cap, so the name (about 115 characters at most) is valid; whether tmux accepts a session name of that length with the `MNGR_PREFIX` is to be verified in phase 4.

Chat rename updates the active agent only, exactly as today's rename does, and leaves archived agents alone.
The taken-names check (`_taken_names_locked`) treats an archived name as taken, which it is.

The auto-name word stays per lane or harness until phase 7, when it becomes a lane-neutral "Chat N", since a chat that has switched harness would otherwise be called after a harness it no longer runs.

### 4.4 Status, stop, start, destroy, rename

- Status is the active agent's: a dead lifecycle is `stopped`, a pending permission is `attention`, thinking or tool-running is `working`, else `idle`.
  During a handoff the chat is `working` (section 5.4), never `stopped`, even though the old agent is stopped part-way through.
- Stop is `mngr stop` of the active agent; start is the in-process ensure-started path for the active agent.
- Destroy is one `mngr destroy --force` naming every member agent, archived ones included.
  Nothing else ever destroys, stops, starts, or messages an archived agent.
  Nothing in the workspace runs `mngr cleanup` or `mngr gc`, so archived agents persist and their transcripts stay readable.
- Rename is the active agent's rename (section 4.3).
- The re-auth restart (`restart_agents_on_account`) is filtered to active agents so a stopped archived agent is never revived by a sign-in.
  The filter is load-bearing, not defensive: since the workspace's create defaults landed, every agent bound to an account carries the `account` label (workers, automations, and the minds app's chats included, not only chats this app created), and an archived agent keeps the label it was created with.
- Every verb but destroy answers 409 with the handoff phase while the chat is converging.

### 4.5 Addressing a chat from inside the workspace

Every chat agent the chat app creates carries `MINDS_CHAT_ID=<chat id>` in its env file (`mngr create --env`), the first agent included.
Agents created outside the chat app (a `mngr create --template chat` from a terminal, the minds app's assist and update-self chats, automations) carry no such variable and are their own chats, so consumers fall back to `MNGR_AGENT_ID`.
Those creates are no longer unbound: a create that names no harness and no account resolves the workspace's default account through `.mngr/settings.local.toml` (written by the chat app's `create_defaults.py` from the account store), carries its `account=<id>` label, and is refused by `system/scripts/require_create_account.py` when no account is signed in.
That changes what they run on, not what they are: they remain own chats with no `MINDS_CHAT_ID`.

- `layout.py` builds the requester address from `MINDS_CHAT_ID`, else `MNGR_AGENT_ID`.
  The shell resolves `self` and attributes ops to clients through that address, so an archived or successor agent's ops land on the chat's tab and client.
- The chat app posts the chat id as the `key` of its client-activity reports, so the shell's attribution log is keyed by chat.
- The `automation` create template's prompt, the caretaker, manage-layout, update-self, and every other skill that names its own chat use `$MINDS_CHAT_ID` with the same fallback.
- Anything that identifies the chat for UI routing outside the workspace carries the chat id: permission requests filed with the latchkey gateway (the minds chrome routes a request to the chat frame whose URL is the chat id), `update_self.py --agent-id` and `surface-chat-tab`, and the file-sharing and workspace requests in the minds-api and migrate-workspace skills.
  Agent ids remain only where mngr itself is the target (`mngr transcript`, `mngr list`, `mngr rsync`, `mngr message` as a backoff).
  The minds desktop client's latchkey handlers use a permission request's `agent_id` twice: to route the request to the chat frame, and to nudge the waiting agent with `mngr message <agent_id>` once it is resolved.
  A request that carries only the chat id would nudge an archived agent after a handoff, so the request carries both (the chat id for routing, the active agent's id for the nudge) or the minds side resolves the chat to its active agent through the chat app; which one is decided in phase 2 (section 9).
- Whether the latchkey gateway's per-agent registration follows a successor agent automatically on discovery is to be verified in phase 4; if not, the handoff registers it.

Messaging a chat from inside the workspace goes through the chat app:

- A script under `system/scripts/` takes exactly one chat id (never a name) and a message, posts to the chat app's loopback message route for that chat (the existing send route, addressed by chat id), and returns the route's verdict.
  It is a drop-in for `mngr message`: it takes the message as `-m`, `--message-file`, or stdin, and exits 0 for delivered or queued, 1 for a failure, and 7 for delivered but blocked on a dialog, which the route already reports as a 500 whose `kind` is `INPUT_BLOCKED`.
  It is standard-library only (`urllib`, `tomllib`), like `require_create_account.py`, because skills run it as `python3 system/scripts/...` and cron runs before any venv; it finds the chat app through the `chat` row of `data/.state/apps.toml`, with `http://127.0.0.1:8010` as the fallback, the way the update-self probes do.
- It falls back to `mngr message <chat id>` only when the connection to the chat app fails or the route keeps answering 404 for a few seconds (an older chat app, or an agent the chat app does not know; a just-created agent is unknown until the observe stream reports it, so a 404 is retried briefly first), never when the chat app answers a refusal, since a refusal during converging is the hold that makes the handoff safe.
  A blocked send (exit 7) is a refusal for this purpose: the text is already in the pane.
  Once the server has accepted the request, the outcome is whatever the route answers, however long it takes: the route blocks through mngr's locked paste-and-confirm for claude and pi, so the script uses a short connect timeout and no read timeout.
- The route answers 503 until the chat app has read its agent list from mngr once, the same rule the instances API follows, so a send during the seconds after a chat-app boot is retried rather than mistaken for an unknown chat and delivered around the app.
  The script retries 503 for a bounded window (the route's own revive budget also surfaces as 503) and then reports a failure.
- The script mints one `message_id` per invocation and sends it on every retry against the chat app, which is the id the route keys its Sending record by (contract A4).
  Delivery is at least once, not exactly once: the script's retries are all on answers that mean nothing was delivered (503), so a duplicate can only come from a caller re-running the script, and that is accepted (an agent sorts out a repeated message).
  A delivered-id ledger that would make a replay a 200 is deferred to phase 4, where held sends are keyed by the same id.
- A send from a script carries no client fields, so the route records no client-activity report for it and it counts as engagement for the memory prioritizer like any other send; nothing on the route changes for that.
  The script offers `--system`, which wraps the text in the system-message sentinel the browser app wraps its nudges in today (`_wrap_system_message`), so the transcript renders a collapsed chip; the wrapping moves into the script and the browser app calls the script.
- Every in-workspace `mngr message` moves to the script, with no exceptions: the browser app's wake, the lead's replies to a worker (`lead-proxy.md`, dead-worker-recovery, update-self, migrate-workspace, fetch-process-show), the task message `create_worker.py` sends after its syncs, and the automation runner's `/clear` and `/<skill>` sends (the route revives a stopped agent on send, which is what the runner's `--start` asked for).
- Workers do not message their lead and gain no wake-up message: the report is a file and the lead polls for it, and a message would land as a user turn in the lead's chat.
  What changes is the address: `create_worker.py` stamps `lead_agent` from `MNGR_AGENT_ID` instead of `MNGR_AGENT_NAME` (the key keeps its name so older task files and workers still parse), so `mngr rsync "$LEAD_AGENT:..."` and `mngr transcript $LEAD_AGENT` survive a rename.
  The same-repo write becomes the primary report delivery for a lead whose work dir is the workspace root (every chat agent's is), with the id-addressed rsync kept for a lead in a worktree.
- Sends that arrive while a chat is converging are held and delivered to the new agent (section 5.7).
- This lands in phase 1, before the rename, addressing by `MNGR_AGENT_ID` (equal to the chat id today), and learns `MINDS_CHAT_ID` in phase 2.

### 4.6 The wire

The chat app pushes one `ChatSnapshot` per chat on its WebSocket (`chats_updated`), with the provisional-chat messages renamed accordingly (`provisional_chat_created`, `provisional_chat_completed`).

```json
{
  "chat_id": "agent-...",
  "title": "Chat 2",
  "name": "Chat-2",
  "project": "inbox",
  "status": "working",
  "labels": {"...": "..."},
  "agent_ids": ["agent-...", "agent-..."],
  "handoff": {"phase": "summarizing", "target_lane": "openai", "target_account_id": "..."},
  "active_agent": {
    "agent_id": "agent-...",
    "name": "Chat-2",
    "harness": "codex",
    "lane": "openai",
    "account_id": "...",
    "state": "RUNNING",
    "activity_state": "THINKING",
    "model_choice": {"...": "..."},
    "queued_messages": [],
    "shoulder_tap_available": false
  }
}
```

- `active_agent` is what the frontend renders the terminal back face (`name`), the model bar (`harness`, `model_choice`), the popups (`harness`), the queue chips, and the tap button from.
  The frontend never calls an agent-keyed route.
- `handoff` is `null` except while converging (section 5.4).
- The Flask state holder currently named `ChatState` is renamed (to `ChatAppState`) so the name is free for chat-level state.

Routes move to `/api/chats/<chat_id>/...` and `/api/chats/create`, with every `/api/agents/...` route kept as an alias that resolves the path parameter as a chat id, until phase 7 drops the aliases.
The `agent_id` field of the create request keeps its name in the alias and is `chat_id` in the new route.
The subagent routes take the three-part key.

### 4.7 The transcript

A chat's transcript is its segments in agent order.

- The per-harness "discover the transcript files, parse them, fill a store" step is lifted out of each watcher into a loader that the watcher's own priming also calls, so an archived segment is loaded by the same code with no thread and no filesystem watches.
  This replaces the alternative of building, priming, and stopping a watcher per archived agent, which was rejected as wasteful.
- An archived segment is immutable, so its event count is recorded on the chat record when the agent is archived.
  Chat-global offsets and totals are sums of the recorded counts plus the live watcher's count, computed without loading anything.
- Segment bodies load lazily, on the first read that needs them (a backfill past the live segment, a jump to an offset inside one), through a bounded thread pool so a long chat with many archived agents loads in parallel.
  Loaded segments are cached with the same eviction the live watcher has (a stopped or destroyed chat drops everything).
- Every event on the wire carries `agent_id`.
  The detail endpoint resolves the segment by event id and re-reads the payload from that segment's files.
  The SSE stream carries only the active agent's events.
- A chat-app-synthesized handoff marker event sits between segments, rendered as a chip ("Switched from Claude to Codex").
  It is a chat-level event type, not a harness `SpecialEventKind`, because harnesses declare their own kinds and this one belongs to none of them.
  Its `event_id` is derived from the chat id and the retiring agent's sequence number, so it is stable across reloads and restarts, as the event-id rule in `harnesses/events.py` requires.
- The subagent endpoints resolve the agent from the three-part key and read its files the same way.

### 4.8 Per-chat state

Presence reports, message stamps, pending permission ids, the auto-open ledger (`auto_open.py`, `data/.apps/chat/auto_opened_chats.json`), and the OOM prioritizer's entries are keyed by chat id.
Where a process is needed (the prioritizer's pid lookup), the chat maps to its active agent.
The auto-open reactor fires for an agent that appears carrying an `auto_open` or `assist` label; a successor agent carries neither, so a handoff never re-pops a tab.

## 5. The handoff

### 5.1 The user's side

- The provider row of the model bar lists every signed-in account.
  Choosing one on a different harness sets the pending lane, shown on the row as "next"; nothing else happens, and the user can keep chatting or change the pending lane again.
  The pending lane is frontend state, like draft text, and is lost on reload.
- The send button reads "Switch and send" while a pending lane differs from the active agent's lane.
- Pressing it opens a two-line confirm: the current agent wraps up and stops, then the conversation continues on the new lane.
  It does not list subagents, workers, or other details.
  Cancel closes the dialog and keeps the pending lane.
- Confirming starts the handoff with the typed message as the first message for the new agent.
  The message shows as a placeholder whose text follows the backend's phase ("Wrapping up with Claude...", "Starting Codex...") rather than a bare "Sending...", because the transitions are long enough that the user should see what is happening.
- Cancel remains available on the page until the old agent is stopped (section 5.6).
- The transcript shows the switch chip once the new agent is active.

### 5.2 Trigger and preconditions

One switch route takes the chat id, the target account, and the message, and dispatches on the target account's harness: the active agent's own harness means a rebind (section 6), any other harness means a handoff.
Two lanes can share a harness (Opencode Go and OpenRouter both run on pi), which is why the dispatch keys on harness rather than lane.
The route refuses with 409 when the chat is already converging, and with 400 when the chat has no active agent (a provisional chat, or one in the failed-next-agent state, which retries through its own route), when the target account is unknown, or when the target account is the active agent's own account.

### 5.3 Draining

`draining` is the first persisted phase, and it has two jobs.
First, it waits, bounded, for any send that is in flight to resolve, the same rule the shoulder tap uses: nothing is switched while a message has not reached a real state.
Second, it returns the queued messages of the active agent to the composer, in send order, on top of whatever is there, exactly as the stop button returns them.
The message that triggered the handoff is not returned; it is held as the new agent's first message (section 5.7).
The queue is returned before anything else so that a later cancel leaves the user's text where it can be seen.

### 5.4 Phases

One enum, shared by the chat record, the instances API mapping, and the frontend, with four values: `draining`, `summarizing`, `switching`, `failed`.
A chat with `handoff == null` is active.
The instance status is `working` for the first three and `error` for `failed`.

The sequence:

1. **draining**: wait for an in-flight send, return the queue (5.3), and hold further sends (5.7).
2. **summarizing**: if a fresh summary exists, skip to 3.
   Otherwise, if the active agent is stopped, start it.
   Then send the summary request through the chat's normal send path and wait (5.5).
   Cancel is still possible here.
3. **switching**: drain-and-stop the old agent: capture the queue under the message lock as a backstop (only a direct `mngr message`, the backoff path, can have parked anything since draining; whatever it finds rides back to the composer too), close any live connection (codex's app-server session), `mngr stop`.
   Then close the open tk steps (5.9), rename and archive the old agent (4.3), record its final event count, and create the new agent (5.8).
   Entering this phase is the point of no return.
4. **active**: the record's `handoff` is cleared, the new agent is the last member, the switch chip is emitted, held sends are delivered, and the chat snapshot is pushed.

Step 3 fails into **failed** (5.10) when the create fails.

### 5.5 The summary

- Path: `data/.apps/chat/chats/<chat_id>/summaries/<seq>.md`, where `seq` is the retiring agent's sequence number.
  The future cache-TTL summarizer writes the same path, so the freshness rule below covers both.
- Freshness: a summary is fresh when its mtime is later than the timestamp of the retiring agent's last user turn that is not itself a summary request.
  The comparison deliberately ignores assistant and tool events, because the request's own turn appends events after the file is written, and a rule keyed on the last event of any kind would call every freshly written summary stale.
  A fresh summary is reused and no request is sent.
- The request is a message to the retiring agent that invokes the `handoff-summary` skill (section 7) with the exact path.
  The skill's content rules (open steps, decisions taken, files touched, running workers, unanswered questions, what the user is waiting for) live in this template, harness-neutral and editable.
- The chat app proceeds to switching as soon as any of these holds:
  the file exists and is non-empty;
  the request's turn settles to idle with no file written;
  the send itself fails, or the reply is an API error event;
  a hard timeout elapses (a few minutes, since a summary can take a while).
  An agent out of tokens, or wedged with a full context window, hits one of the first three within seconds, so the switch is immediate.
- If the transcript gained user turns after the summary file's mtime (only external senders can add them, because the chat app holds its own sends), one addendum is requested with a shorter bounded wait; the switch proceeds regardless of whether it lands.
- A stopped active agent is started for the request.
  Only when it cannot be started at all does the handoff proceed without a summary.
- Whether a summary was produced, reused, or missing is recorded on the record's `handoff` entry and told to the new agent.

### 5.6 Cancel

Cancel is accepted in `draining` and `summarizing`: the record's `handoff` is cleared, the trigger message returns to the composer (it was typed for the new lane, so it is not delivered to the agent the user chose to leave), every other held send is delivered to the old agent in order, and the summary turn, if one is running, finishes as an ordinary turn.
The queued messages already returned to the composer stay there; the user resends what they still want.
The pending lane is kept in the UI so the user can try again.
Cancel is refused with 409 in `switching` and later.

### 5.7 Held sends and the trigger message

- From `draining` until the new agent is active, every send to the chat (from the composer, from a worker through the script, from anything else) is held: recorded as "Sending" in the chat app, shown with the phase text, and delivered in order to the new agent right after the handoff prompt.
  This satisfies the conservation contract: the message is continuously visible and the backend, not the frontend, resolves the placeholder.
- The trigger message is the first of the held sends.
  It rides the new agent's `mngr create --message` together with the handoff prompt (5.8); the others follow through the normal send path once the agent is ready.
- A cancel returns the trigger message to the composer and delivers the other held sends to the old agent (5.6).
- Held sends survive a chat-app restart because they are persisted on the record's `handoff` entry.

### 5.8 Creating the new agent

- The new agent id is minted and recorded on the record's `handoff` entry before the create runs, and passed with `mngr create --id`, so a resume can tell whether the create landed.
- The create is the existing chat create command: the reused mngr name, `--type <harness>`, `--template chat`, the account binding args from `binding.create_args`, `--label user_created=true`, `--label display_name=<title>`, `--label project=...`, `--label account=...`, plus `--label chat_id=<chat id>`, `--label chat_seq=<n>`, `--env MINDS_CHAT_ID=<chat id>`, and `--message <handoff prompt>`.
  `--message` delivers after the harness signals readiness, the path `/welcome` already takes.
  The `--type` and the binding args are always explicit, never left to config: `.mngr/settings.local.toml` supplies the workspace's *default* account to a create that names none, and the handoff's target is usually not the default; CLI list flags append after the file's, so an explicit `--type` and `--env` win for the same variable or path.
  `require_create_account.py` gates every in-workspace create, this one included, on the local file naming a type: a handoff on a workspace whose last account was just removed is refused by the gate rather than by the chat app, and that verdict is what the failed-next-agent state shows (5.10).
- The handoff prompt is built once, stored on the `handoff` entry, and resent verbatim on every retry, whatever lane the retry uses.
  It contains: the summary path, or the statement that the predecessor did not produce one; the predecessor's archived mngr name, agent id, and state dir, plus the same for every earlier member, so `mngr transcript` and the find-transcripts skill reach them; the titles of the steps closed at handoff; the lanes involved; and then the user's message.
  Its text is a reference document in this template that the chat app fills in, so it stays harness-neutral and editable.
- The first-chat claim is never taken for a successor: the claim is only attempted for a create with an empty message, and a handoff always passes one, so the `first` template and `/welcome` stay with the workspace's first chat.

### 5.9 tk steps, workers, subagents

- Open step records of the retiring agent are closed during switching with a summary saying the conversation moved to a new harness.
  tk scopes step records by creator (`MNGR_AGENT_NAME`), so the chat app runs `tk` with that variable set to the retiring agent's pre-archive name and `TICKETS_DIR` set as the agents have it, and does so before or after the rename indifferently, since the name comes from the environment rather than from mngr.
  Their titles ride the handoff prompt; the new agent creates whatever steps still apply.
  Adopting steps across agents would need tk changes (creator scoping is by agent name) and is not done.
- Workers keep running.
  They report by writing into the shared work dir and messaging the chat through the script (4.5), so their messages are held during converging and reach the new agent.
  Nothing revives the archived agent, because nothing addresses it.
- Subagents die with the retiring agent's process.
  The summary skill asks the agent to mention work its subagents were doing.

### 5.10 Failure of the create

The chat then has no running agent and one archived agent more.
The record's `handoff` entry moves to `failed` with the reason (mngr's exit status and the last lines it printed, as today's failed provisional create records, which is also how the create gate's "No provider account is signed in on this machine" reaches the page), the pre-minted id, and the stored prompt.
The instance status is `error`, the page shows the reason over the composer with a retry that offers every signed-in account, and a retry on any lane runs step 3's create again with the same prompt.
The archived transcript stays readable underneath.
Destroy remains available.

### 5.11 Durability and resumption

The transition survives a chat-app restart at any point, an OOM shed, or an update restart.
The record's `handoff` entry persists the phase, the target, the pre-minted id, the held sends, and the summary outcome, and every step re-checks reality before acting, so a resume runs the steps again and each one either finds its work done or does it:

- `draining` resumes by re-checking the queue.
- `summarizing` resumes by re-checking the summary's freshness, then the proceed conditions.
- `switching` resumes by checking, in order: is the old agent stopped (else stop it); does an agent with the archival name exist (else rename); does it carry `archived_at` (else archive); are its steps closed; is `final_event_count` recorded; does an agent with the pre-minted id exist.
- If an agent with the pre-minted id exists and is running or stopped, it is adopted as the new active agent and the handoff completes.
  If the create left something mngr refuses to reuse (`mngr create --id` raises `DuplicateAgentIdOnHostError` for an id present on the host), the partial agent is destroyed and the create rerun under the same id.
- `failed` resumes as failed.

Reconciliation is chosen over replay because every step is idempotent when checked against mngr's own state, and because a replayed step could double-send the summary request or double-archive.

### 5.12 What the model bar shows

A harness handoff resets model, effort, and fast mode to the new harness's defaults, since those live in the agent's own settings; no translation between harness catalogs is attempted.

## 6. Rebind

A rebind is the same gesture applied to an account on the active agent's own harness.

- Sequence: `mngr stop` the agent; rewrite the binding directly in the agent's state dir (the `CLAUDE_CONFIG_DIR` line of the env file for claude, the credential symlink for codex, pi, and antigravity, exactly what `binding.create_args` writes at create, from the per-harness tables in `harnesses/account_scope.py`); `mngr label account=<new account id>`; `mngr start --no-resume`.
  The edit lands while the agent is down so the restart sources it, which is why `--restart` is not used.
  No mngr command is added for the edit; the env file is the interface.
- The transcript, the tk steps, and the model settings carry over.
- The confirm dialog's rebind variant says only that the agent restarts on the new account.
- Feasibility per harness is unverified: whether each harness resumes its session cleanly under a swapped credential is tested in phase 6, and a harness that does not falls back to a handoff, which the dialog then says.
- Before the stop: the bounded wait for an in-flight send, the queue returned to the composer, and any live connection closed, as in a handoff's draining and switching.
- The trigger message is held through the restart and delivered through the normal send path once the agent is ready, shown with the same phased placeholder.
- A rebind is persisted on the record too (a `rebind` entry with the target account and the held sends), and resumes the same way: stopped, repointed, relabeled, started, each step re-checked.

## 7. The template's side: AGENTS.md and the summary skill

- `AGENTS.md` gains a section every agent reads: when `MINDS_CHAT_ID` is set and differs from `MNGR_AGENT_ID`, the agent is a successor in an existing chat; if its first message carries no summary pointer, it gathers its own context by listing its predecessors (`mngr list --include 'labels.chat_id == "$MINDS_CHAT_ID"'`) and reading their transcripts (`mngr transcript`, or the find-transcripts skill).
  This is the backstop for every failure mode of the summary, and it costs nothing when the summary exists.
- A `handoff-summary` skill for the retiring side: the slash command the chat app sends, naming the output path; the skill writes the summary and stops.
  It is harness-neutral (every harness in this template expands slash commands or is told to).
- A `continue-chat` reference document: the handoff prompt template the chat app fills in (5.8).
- The find-transcripts skill learns the `chat_id` label and the archival name shape.

## 8. Phases

Each phase is one pull request on this repository, merged before the next starts, and leaves every existing test green.
Phases 1 and 2 change no behavior.
Phases 3 through 6 add behavior and are exercised by hand in a dev workspace before the next starts.
Where the minds repo is touched, the paired branch is named.

### Phase 1: chats are messaged through the chat app

- The `system/scripts` messaging script (4.5): one chat id, the message as `-m`, `--message-file`, or stdin, the `--system` wrap, the loopback route found through the registry, the caller-minted `message_id` reused across retries, `mngr message` exit codes, and the backoff to `mngr message` only on a failed connection or a 404.
  In this phase the chat id it takes is `$MNGR_AGENT_ID`.
- The chat app's message route: 503 until the agent list is known (4.5).
  Nothing changes about how a send with no client fields is recorded.
- Every in-workspace `mngr message` switches to the script: the browser app's wake (which hands its sentinel wrapping to the script), the lead-to-worker replies in `lead-proxy.md`, dead-worker-recovery, update-self, migrate-workspace, and fetch-process-show, `create_worker.py`'s task message, and `run_automation.sh`.
- The worker path (4.5): `create_worker.py` stamps `lead_agent` with the lead's agent id, `worker-reporting.md` makes the same-repo write the primary delivery with the id-addressed rsync as the worktree case, and `transcript-exploration.md` reads the lead's transcript by that id.
- Tests: the script against a stub chat app (delivered, blocked, refused, 503 then delivered, unreachable, and 404); the route's 503 gate in the chat app's suite; `create_worker_test.py` asserting the stamped id; one integration test under the vendored mngr that renames a local agent and shows `mngr rsync` and `mngr transcript` still resolve it by id.
- Exit check: a worker's report reaches a lead that was renamed mid-task, its `mngr transcript $LEAD_AGENT` still reads, and a lead's reply reaches the worker through the chat app.

### Phase 2: the rename

- `ChatId` in a new `imbue/chat/primitives.py`, distinct from `AgentId`; a bridging rule that a chat's id equals its first agent's id, marked `CLEANUP` where it is assumed.
- Chat-level names on instance records, provisional records, the display name, the project label, presence, message stamps, pending permissions, and the OOM prioritizer (4.8).
- `AgentManager`'s chat-level duties (naming, provisional chats, snapshots, nudges, status) and agent-level duties (observe, mngr commands, trackers, sessions, model watchers) named apart and grouped; a physical split into two classes is allowed where it is mechanical but not required in this phase.
  `ChatState` renamed `ChatAppState`.
- The `ChatSnapshot` wire shape with `active_agent` (4.6), the renamed WebSocket messages, the chat-keyed routes with the agent-keyed aliases.
- The frontend: `agentId` becomes `chatId` in every model and view, `AgentManager.ts` becomes the chats model, `ProtoAgent` becomes a provisional chat, and every agent-level read comes from `active_agent`.
- `MINDS_CHAT_ID` on every chat-app-created agent; `layout.py`, the phase 1 script, `create_worker.py`'s `lead_agent` stamp, the automation prompt, and the skills prefer it with the `MNGR_AGENT_ID` fallback; the client-activity key becomes the chat id.
- The auto-open reactor's ledger and address (4.8) keyed by chat id.
- The minds side's decision on the permission request's ids (4.5): both ids on the request, or a chat-to-active-agent lookup through the chat app, in the paired minds branch.
- Subagent keys become `<chat_id>.<agent_id>.<session_id>`.
- The transcript loader lifted out of each watcher, and the segment facade in front of it with exactly one segment.
- The handoff phase enum declared and carried as `null`.
- Contracts section 4.3, the meta spec's section 7.4, the chat README, and the paired minds glossary entry updated to the new vocabulary.
- The only externally visible differences: the new env variable, the new subagent key shape, the renamed WebSocket messages (the pages are served by the same process, so no cross-version skew), and the new routes beside the aliases.
- Exit check: every existing test passes under the new names, the minds e2e suites pass unchanged against the aliases, and no behavior differs.

### Phase 3: a chat can have several agents, read side

- The chat record store (4.2), the own-chat rule, membership from the record with archived agents excluded from the instance list, the `chat_id` and `chat_seq` labels read back.
- Status, stop, and start from the active agent; destroy over every member; the re-auth restart filtered to active agents (4.4).
- Multi-segment transcript reads: recorded counts, lazy parallel loading, `agent_id` on every event, detail and subagent reads resolved by segment, the switch chip's event type (4.7).
- Nothing writes a record except tests, through the store interface.
- Exit check: a hand-built two-member record renders one continuous transcript with correct offsets, paging, jumps, detail fetches, and per-segment subagent views; stop, start, destroy, and rename act on the right agents.

### Phase 4: the handoff, backend

- The handoff route, the phases, draining, the summary request and its proceed conditions, freshness reuse, starting a stopped agent, the addendum, closing steps, the archive rename and label, the pre-minted id and create, the stored prompt, the failed state with retry, held sends, cancel, refused verbs, and resumption (section 5).
- The `handoff-summary` skill, the `continue-chat` reference, and the `AGENTS.md` section (section 7).
- To verify during the phase: tmux's acceptance of the archival session name; latchkey registration of the successor; `DuplicateAgentIdOnHostError` handling on resume.
- Tests: the conservation suites gain handoff cases (queue returned, trigger held, held sends delivered, cancel during summarizing); the storm test covers restart mid-handoff in every phase; an API-driven e2e with two real accounts on different harnesses.
- Exit check: a handoff driven through the API completes, resumes after a chat-app kill in each phase, and fails over to the failed state and retries when the create is made to fail.

### Phase 5: the handoff, UI

- The pending lane on the provider row, "Switch and send", the two-line confirm, the phased placeholder text, the switch chip, the failed page with retry on another lane, the model bar reset, and the 409 handling for stop, start, and rename while converging (5.1).
- Exit check: the two-account e2e drives the whole flow through the browser, including cancel during summarizing and a retry on a third lane after a forced failure.

### Phase 6: rebind

- The rebind sequence (section 6), the dialog variant, per-harness verification, the fallback to a handoff.
- Independent of phases 4 and 5 once phase 3 is in; it never creates a new agent.
- Exit check: one e2e per harness that has a second account available.

### Phase 7: cleanup

- Drop the `/api/agents/*` aliases; retarget the minds_evals bridge, the minds deployment tests, and the e2e runner in the paired minds branch, which merges after this template is tagged, as the app-model arc did.
- The lane-neutral auto-name word.
- Remove the phase 2 `CLEANUP` bridges, update the minds docs and glossary, and record the settled decisions.

## 9. External callers and compatibility

| Caller | Today | After phase 2 | After phase 7 |
|---|---|---|---|
| minds e2e runner (`e2e_workspace_runner.py`) | finds the chat frame by `/agent-<hex>/` | unchanged (chat ids keep the prefix) | unchanged |
| minds_evals bridge (`minds_bridge.py`) | `/api/agents/create-chat`, `/api/agents/<id>/message`, `/events` | served by the aliases | retargeted to `/api/chats/...` |
| minds deployment tests | `/api/agents/...` | aliases | retargeted |
| minds assist and update-self chats | a bare `mngr create --template chat` inside the workspace, bound to the default account and harness through `.mngr/settings.local.toml`, carrying `account=<default>` | own chats, no `MINDS_CHAT_ID` | unchanged |
| the `automation` template prompt | `app:chat?instance=$MNGR_AGENT_ID` | `$MINDS_CHAT_ID` with fallback | unchanged |
| the shell | instance keys, addresses, `/api/client-activity` keys | chat ids, which equal today's keys | unchanged |
| the minds chrome's permission routing | request `agent_id` = chat frame URL | chat id | unchanged |
| the minds latchkey handlers' resolution nudge (`mngr message <agent_id>`) | the request's `agent_id` | the active agent's id, carried on the request beside the chat id or looked up through the chat app (4.5) | unchanged |

## 10. Open questions and things to verify

- Whether tmux accepts a session name of about 120 characters under `MNGR_PREFIX` (phase 4).
- Whether latchkey's per-agent registration follows a successor automatically on discovery, or the handoff must register it (phase 4).
- Whether the minds side treats a permission request's `agent_id` purely as a routing key (safe to carry the chat id) or also as an authorization key against the registered-agent set; if the latter, requests carry both the chat id and the agent id (phase 2).
- Per-harness session resumption under a swapped credential for the rebind (phase 6).
- Settled for phase 1 (4.5): the same-repo write is the primary report delivery when the lead's work dir is the repo's main worktree, and a lead in a worktree of its own is reached by the id-addressed `mngr rsync`; whether that second path is ever exercised in practice is to be confirmed by the launch-task suite.
- The exact wording and placement of the `AGENTS.md` section and the summary skill's content rules (phase 4, with the user).

## 11. Documents this plan revises

- `docs/system/blueprint/workspace-app-model/contracts.md` section 4.3: the chat row's key, title, status, create, delete, stop, and start columns.
- `docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md` section 7.4.
- `system/apps/chat/README.md`.
- The minds repo's `apps/minds/docs/workspace/glossary.md` "chat agent" entry (rewritten for the local-settings account model since this plan was drafted, but still "one per chat tab"), and the mngr-side changes note in `docs/system/blueprint/workspace-app-model/mngr_side_changes.md`.
- `system/apps/chat/README.md`'s provider-accounts section, which now also describes `.mngr/settings.local.toml` and the auto-open reactor; both paragraphs are agent-keyed today.
