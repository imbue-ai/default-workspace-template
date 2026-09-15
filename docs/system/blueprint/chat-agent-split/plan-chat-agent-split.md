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
12. **tk step records follow the chat, not the agent.**
    tk scopes step records by creator, and the creator is the chat's id (`MINDS_CHAT_ID`, falling back to the agent's name outside the chat app), so a successor sees its predecessor's open steps as its own and decides which still apply; the handoff closes nothing.
    Keying on the chat id also fixes the rename bug the name-based scoping had: `mngr rename` changes `MNGR_AGENT_NAME` under a running process, which stranded the steps created after a rename.
13. **Rebind and handoff are different operations** behind one user-facing gesture ("switch provider").
    Rebind keeps the agent, its transcript, its tk steps, and its model settings; a handoff replaces the agent.
    Where a harness cannot resume a session under a swapped credential, a rebind falls back to a handoff.
14. **Summarize-first, and reuse an existing fresh summary.**
15. **Name-based addressing of chats is retired.**
    In-workspace senders message a chat through the chat app by chat id, with `mngr message` only as a backoff when the chat app cannot be reached.
    The rule has no exceptions: a worker and an automation agent are chats under the own-chat rule, so a lead's reply to a worker goes through the chat app too.
    Worker skills need no instructions about the handoff window because the chat app holds sends during it.
    Where mngr itself is the target (`mngr rsync`, `mngr transcript`), the lead is named by its agent id, which a rename does not change.
16. **The new agent receives as much context as could be relevant**, through `mngr create --message`: the summary in full with its path, or the note that none exists, the archived agent's identity and transcript locations, the closed steps, and the user's message.
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
28. **Phase 7 dropped every `/api/agents/...` alias at once**, with no release of overlap.
    No shipped template ever had them (phases 1 through 7 ship together), and the one caller that could run a stale copy of the messenger, a worker's worktree cut before an update, only ever messages another worker, which is its own chat, so its 404 fallback to `mngr message` lands on the right agent anyway.
29. **`GET /api/agents` stays.** It is the plain listing of every mngr agent, not an alias: it is how loopback callers (the evals bridge, the deployment tests) see background agents beside the chats, and `/api/chats` lists chats only.
30. **One auto-name word, "Chat"**, for every harness and lane (4.3); chats named before phase 7 keep the names on their agents.
31. **A browser is owned by a chat**, not by the agent that claimed it: the fleet CLI sends `MINDS_CHAT_ID`, with `MNGR_AGENT_ID` as the fallback for a background agent, so the daemon's wake-ups reach the chat through the chat app whichever agent runs it.
32. **The paired minds branch merges after the template is tagged**, as the app-model arc's did: the evals bridge clones the template's `main`, which has no `/api/chats` until the whole arc lands there.

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

The `rebind` entry (section 6) sits in the same place, and a record carries at most one of the two:

```json
{
  "rebind_id": "...",
  "phase": "restarting",
  "started_at": "...",
  "target_lane": "anthropic",
  "target_account_id": "...",
  "target_harness": "claude",
  "target_label": "Anthropic 2 (Claude Code)",
  "agent_id": "agent-<the active agent>",
  "previous_account_id": "...",
  "previous_lane": "anthropic",
  "claude_sessions_config_dir": "/home/user/.minds/accounts/<previous, then the target once the files moved>",
  "restarted_account_id": null,
  "trigger_message_id": "...",
  "trigger_text": "the user's message",
  "held_sends": [{"message_id": "...", "text": "...", "origin": "client"}],
  "returned_block": "",
  "error": null
}
```

The two entries share their common fields (`ChatTransitionRecord` in code): the phase, the target, the confirming message, the held sends, the returned queue, and the error.
A rebind changes no membership; when it completes on a chat of one agent the record is dropped again, so a record still exists only for a chat that has had a handoff.

The own-chat rule: when listing instances, every non-primary agent that appears in no record as a non-first member is a chat of its own.
Archived agents are excluded from the instance list because the record names them, not because of the `archived_at` label.
A bare `mngr list` does not hide `archived_at` agents (only `--active` excludes them), so the chat app must never rely on mngr's listing to filter them.

### 4.3 The active agent and its name

The chat's mngr name is the canonical form of its title ("Chat 2" is the `display_name` label and `Chat-2` the mngr name), exactly as today.
Every agent that becomes active takes that name, which keeps the terminal back face, the git commit identity (`agent_rewrite_bash_command.py` reads the live name), and `mngr list` readable.
Name reuse is not load-bearing for correctness: nothing may depend on reaching the chat by that name (section 4.5).

The retiring agent is renamed to `archived-<seq>-<canonical>-<agent-id>` with one `mngr rename` whose `-l` flags carry every label in the same atomic write: `display_name="<title> (archived <seq>)"`, `chat_id`, `chat_seq`, and `archived_at` (the label `mngr archive` would set; the rename sets it itself, so the archive is one mngr call rather than three).
The archival prefix sorts archived agents together and the sequence number orders them; the agent id keeps the name unique for the life of the host.
`AgentName` allows letters, digits, dashes, and underscores with no length cap, so the name (about 115 characters at most) is valid.
The rename happens after `mngr stop`, so no tmux session carries the archival name, and an archived agent is never started; tmux's acceptance of a name that long only matters to someone starting one by hand.

Chat rename updates the active agent only, exactly as today's rename does, and leaves archived agents alone.
The taken-names check (`_taken_names_locked`) treats an archived name as taken, which it is.

Since phase 7 every auto-minted name is a lane-neutral "Chat N" (`naming.py`'s one `AUTO_NAME_WORD`), whatever harness or lane the chat starts on, since a chat that has switched harness would otherwise be called after a harness it no longer runs; the names chats were given before that ("Codex 1", "Pi 2") stay on their agents, and new numbers skip every taken name.

### 4.4 Status, stop, start, destroy, rename

- Status is the active agent's: a dead lifecycle is `stopped`, a pending permission is `attention`, thinking or tool-running is `working`, else `idle`.
  During a handoff the chat is `working` (section 5.4), never `stopped`, even though the old agent is stopped part-way through.
- Stop is `mngr stop` of the active agent; start is the in-process ensure-started path for the active agent.
- Destroy is one `mngr destroy --force` naming every member agent, archived ones included.
  Nothing else ever destroys, stops, starts, or messages an archived agent.
  Nothing in the workspace runs `mngr cleanup`, and the `mngr gc` that follows a destroy collects only orphaned worktrees, the snapshots and machines of destroyed hosts, and unreferenced volumes, never a live agent, so archived agents persist and their transcripts stay readable.
- Rename is the active agent's rename (section 4.3).
- The re-auth restart (`restart_agents_on_account`) is filtered to active agents so a stopped archived agent is never revived by a sign-in.
  The filter is load-bearing, not defensive: since the workspace's create defaults landed, every agent bound to an account carries the `account` label (workers, automations, and the minds app's chats included, not only chats this app created), and an archived agent keeps the label it was created with.
- Every verb but destroy answers 409 with the handoff phase while the chat is converging.
  The answer's `detail` names the destination (the harness for a handoff, the account for a rebind) and the phase in plain words ("This chat is switching to Codex and is summarizing; wait for the switch to finish, then try again.", "This chat is switching to Anthropic 2 (Claude Code) and is restarting; ..."), since the shell's tab menu and the chat page both show it to the user as is; nothing greys those controls out first.

### 4.5 Addressing a chat from inside the workspace

Every chat agent the chat app creates carries `MINDS_CHAT_ID=<chat id>` in its env file (`mngr create --env`), the first agent included.
Agents created outside the chat app (a `mngr create --template chat` from a terminal, the minds app's assist and update-self chats, automations) carry no such variable and are their own chats, so consumers fall back to `MNGR_AGENT_ID`.
Those creates are no longer unbound: a create that names no harness and no account resolves the workspace's default account through `.mngr/settings.local.toml` (written by the chat app's `create_defaults.py` from the account store), carries its `account=<id>` label, and is refused by `system/scripts/require_create_account.py` when no account is signed in.
That changes what they run on, not what they are: they remain own chats with no `MINDS_CHAT_ID`.

- `layout.py` builds the requester address from `MINDS_CHAT_ID`, else `MNGR_AGENT_ID`.
  The shell resolves `self` and attributes ops to clients through that address, so an archived or successor agent's ops land on the chat's tab and client.
- The chat app posts the chat id as the `key` of its client-activity reports, so the shell's attribution log is keyed by chat.
- The `automation` create template's prompt, the caretaker, manage-layout, update-self, and every other skill that names its own chat use `$MINDS_CHAT_ID` with the same fallback.
- Anything that identifies the chat for UI routing outside the workspace carries the chat id: permission requests filed with the latchkey gateway (the minds chrome routes a request to the chat frame whose URL is the chat id), `update_self.py surface-chat-tab --chat-id`, and the file-sharing and workspace requests in the minds-api and migrate-workspace skills.
  Agent ids remain only where mngr itself is the target (`mngr transcript`, `mngr list`, `mngr rsync`, `mngr message` as a backoff).
  A permission request's `agent_id` field is the chat id: the gateway checks only its shape and rejects extra fields, the minds chrome routes a request by workspace and matches its card by `request_id`, and the id is never an authorization key.
  The minds desktop client's latchkey handlers also use that id to nudge the waiting agent with `mngr message <agent_id>` once the request is resolved, which after a handoff would reach an archived agent (archiving is a label plus a stop, so the agent stays discoverable and the nudge lands on a stopped pane).
  That nudge moves to the chat: `mngr exec <chat id> -- python3 system/scripts/message_chat.py <chat id> -m ...`, which the chat app delivers to the chat's active agent, with a label-aware fallback when the id names no live agent; it lands in phase 4's paired minds branch, since until then every chat's id is its live agent's.
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
  What changes is the address: `create_worker.py` stamps `lead_agent` from `MNGR_AGENT_ID` instead of `MNGR_AGENT_NAME` (the key keeps its name so older task files and workers still parse), so `mngr transcript $LEAD_AGENT` survives a rename.
  `lead_agent` stays an agent id, not a chat id: it names the agent that dispatched the worker, and mngr, whose transcript the worker reads, knows only agents (a chat is the chat app's notion).
  The report is delivered by a write into the lead's checkout: `create_worker.py` stamps `lead_work_dir` from `MNGR_AGENT_WORK_DIR` beside the id, and the worker copies its report there, falling back to the repo's main worktree (every chat agent's work dir) when the field is absent; there is no `mngr rsync` delivery path.
  Reading a chat's earlier segments (the lead's predecessors) is a phase 4 concern: the worker lists them by the `chat_id` label and reads their transcripts.
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
  "handoff": {"kind": "handoff", "phase": "summarizing", "target_lane": "openai", "target_account_id": "...",
              "target_harness": "codex", "target_label": "Codex",
              "held_sends": [{"message_id": "...", "text": "the confirming message"}], "error": null},
  "active_agent": {
    "agent_id": "agent-...",
    "name": "Chat-2",
    "harness": "codex",
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
  It carries no `lane`: the lane is the account's, and the page reads it off the account row it already holds for `account_id`, which is all the switch rule needs (5.1: any account but the chat's own is a switch; the kind, a rebind or a handoff, is read by harness and lane, since two lanes can share one harness).
  The frontend never calls an agent-keyed route.
- `handoff` is `null` except while converging (section 5.4, section 6): one shape for both kinds of switch, since the page renders them the same way.
  While set it carries `kind` (`handoff` or `rebind`), the phase (`restarting` is the rebind's), the target lane, account, and harness, `target_label` (what the phase text and the 409s name the destination by: the harness for a handoff, the account for a rebind), the messages held for after the switch (the confirming message first, so a reloaded page keeps showing them until they land in the transcript; the record keeps the confirming text as `trigger_text` once it leaves the held list), and the failed phase's `error`.
- The Flask state holder currently named `ChatState` is renamed (to `ChatAppState`) so the name is free for chat-level state.

Routes are `/api/chats/<chat_id>/...` and `/api/chats/create`; every `/api/agents/...` route was kept as an alias that resolved the path parameter as a chat id until phase 7 dropped the aliases (`GET /api/agents`, the plain listing of every mngr agent, is not one and stays).
The create request's minted-id field is `chat_id`.
The subagent routes take the three-part key.

### 4.7 The transcript

A chat's transcript is its segments in agent order.

- The per-harness "discover the transcript files, parse them, fill a store" step is lifted out of each watcher into a loader that the watcher's own priming also calls, so an archived segment is loaded by the same code with no thread and no filesystem watches.
  This replaces the alternative of building, priming, and stopping a watcher per archived agent, which was rejected as wasteful.
- An archived segment is immutable, so its event count is recorded on the chat record when the agent is archived.
  Chat-global offsets and totals are sums of the recorded counts plus the live watcher's count, computed without loading anything.
- Segment bodies load lazily, on the first read that needs them (a backfill past the live segment, a jump to an offset inside one), one at a time: a read reaches back one segment at a time, and keeping archived history out of memory is the point, so phase 3 chose sequential loading over the thread pool first sketched here.
  Loaded segments are cached with the same eviction the live watcher has (a stopped or destroyed chat drops everything).
- Every event on the wire carries `agent_id`.
  The detail endpoint resolves the segment by event id and re-reads the payload from that segment's files.
  The SSE stream carries only the active agent's events.
- A chat-app-synthesized handoff marker event (`agent_switch`) sits between segments, rendered as a chip ("Switched from Claude to Codex"); since phase 8 it carries the message the user switched with, when the handoff folded that message into the successor's prompt, and the page opens the successor's segment on it as the user's own bubble.
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

As landed in phase 5:

- Pressing an account on another harness in the provider menu sets the pending lane and closes the menu; the row wears a "next" badge, the Provider row reads "Claude Code, next: OpenAI (Codex)", and pressing the row again, or the account the chat runs on, takes the choice back.
  Since phase 6 every account but the chat's own is a switch target: one on the chat's own harness and lane makes the next send a rebind (section 6), any other a handoff; a sign-in from the menu's "+ Add a provider" sets the lane the same way, and the confirm offers "Start a new chat instead" for the sibling chat the retired "Launch a new chat?" prompt used to open.
  The pending lane is spent once the chat runs on that account, or on a new agent at all (a failed switch retried on another account lands there); a cancelled switch keeps it.
- The send button becomes a text pill reading "Switch and send" while a lane is pending; it appears, like the plain send button, only once there is something to send.
  Enter does the same as the button.
- The confirm is the workspace's notice dialog: "Switch to Codex?", then "Claude wraps up what it is doing and stops." and "The conversation continues on OpenAI (Codex), starting with your message."
  Cancel keeps the pending lane; Switch and send posts the handoff with the attachment-expanded text, a send-time `message_id`, and the client fields, and puts the `returned_block` back in the composer.
- The typed message is painted as the page's own not-yet-real bubble carrying its `message_id`, which stands down the moment the snapshot lists the send under `handoff.held_sends`; from then on the held messages render from the snapshot with the phase as their caption ("Wrapping up with Claude...", "Claude is writing a summary...", "Starting Codex...", "Could not start Codex").
  When the switch ends they come back as the page's bubbles until their turns arrive in the successor's transcript; a cancelled switch's confirming message goes back to the composer instead.
  Every send while converging carries its `message_id` too and is shown the same way.
- The activity strip shows the phase instead of the retiring agent's turn; the composer's placeholder says a message typed now is delivered once the new harness is ready.
- The Stop button is replaced by "Cancel switch" while the switch can still be called off (draining and summarizing) and hidden afterwards; a cancel puts the returned confirming message back in the composer.
- The failed page (5.10) is a notice over the composer: "Could not start Codex", mngr's reason, a picker of every signed-in account defaulting to the failed target, and "Try again"; a refused retry shows its reason under the picker.
- The controls a converging chat refuses (the Stop agent row, the shoulder tap, the model picker, and the shell's stop, start, and rename) stay live and show the 409's detail through their existing alerts and notices (4.4).
- An optimistic model pick made for the old agent is forgotten when the chat's active agent changes, so the bar shows the new harness's own choice (5.12).

As landed in phase 8, which revised the flow above after use:

- Pressing an account opens the switch dialog at once rather than arming a "next" lane behind the menu: "Switch to Codex?" for a handoff (with a model picker for the successor, fed by `GET /api/accounts/<account_id>/model-options`), one line for a rebind (no picker: the agent keeps its settings).
  "Switch this chat" is the primary action and arms the switch; "Start a new chat" is secondary and opens a chat on that account and model with the composer's draft moved over; Cancel leaves everything as it was.
- Armed, the composer shows a strip ("Your next message switches this chat to OpenAI (Codex), GPT-6 Astra · High") with Change and Cancel, the model bar's chip and card read as the target, and Switch and send carries the switch out with no second confirmation.
- A chat with no genuine user turn (by the rule `has_user_turn` shares with the summary freshness check) gets no dialog: the switch runs at once as a fresh start (5.5, 5.8), with the draft left in the composer.
- The handoff is one node in the transcript rather than a chip and a write: "Handing off to Codex…" while it runs (from the snapshot until the summary request is on the stream, then anchored on it), "Handed off from Claude to Codex" once the `agent_switch` event lands, expandable to the summary turn and the successor's handoff prompt, "Handoff called off" for a cancelled one. A landed switch draws a rule under the node, so the boundary between the two agents' segments reads at a glance. The held bubbles read "Sending…" like any send.
- The failed page names the step: "Could not start Codex" or "Could not set the model on Codex".
- The handoff prompt, now a user message in the successor's transcript (it arrives through the send path), renders as a collapsed "Handoff prompt" chip inside the handoff node, after the summary turn, rather than as the first item of the successor's segment; it still counts as a genuine user turn for the fresh-start rule, since it carries the message the user switched with and the summary. The message the user switched with, folded into that prompt, is shown as the successor's opening bubble from the `agent_switch` marker (4.7), and its held "Sending…" bubble stands down once the marker is on the stream; the prompt's own arrival does not count as a sent message's.

### 5.2 Trigger and preconditions

One switch route (`POST /api/chats/<chat_id>/handoff`, body `account_id`, `message`, `message_id`, and the client fields a send carries) takes the chat id, the target account, and the message, and dispatches on the target account's harness and lane: the active agent's own harness and lane, on a harness listed in `harnesses/binding.py`'s `REBIND_VERIFIED_HARNESSES`, means a rebind (section 6); anything else means a handoff.
Two lanes can share a harness (Opencode Go and OpenRouter both run on pi) with different model sets, and a rebind keeps the model settings (principle 24), so a move between them replaces the agent rather than restarting it under a model the new provider may not serve; the first draft of this section keyed the dispatch on harness alone, which phase 6 changed for that reason.
The answer's `kind` says which it was.
The route refuses with 409 when the chat is already converging (the failed phase included, which retries through its own route), with 404 when the chat has no active agent (a provisional chat, or an id that names no chat), and with 400 when the target account is unknown or is the active agent's own account.
The route runs draining on the request's thread and answers 202 with the phase the chat is then in and the queued text draining returned for the composer; the remaining phases run on a thread of the chat app's.
Cancel is `POST .../handoff/cancel` and the retry of a failed create `POST .../handoff/retry` with an `account_id`.

### 5.3 Draining

`draining` is the first persisted phase, and it has two jobs.
First, it waits, bounded, for any send that is in flight to resolve, the same rule the shoulder tap uses: nothing is switched while a message has not reached a real state.
Second, it returns the queued messages of the active agent to the composer, in send order, on top of whatever is there, exactly as the stop button returns them.
A confirmed switch is a stop: the queue cannot be taken out of a live turn without ending it (on claude it lives inside the process), so draining is the stop button's own interrupt-to-composer, which also waits out an in-flight send under the message lock, and the summary request then lands on an idle agent at once.
A stopped agent has nothing to drain.
The message that triggered the handoff is not returned; it is held as the new agent's first message (section 5.7).
The queue is returned before anything else so that a later cancel leaves the user's text where it can be seen; the returned text also stays on the record's `handoff` entry (`returned_block`) for a page that reloads.

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
3. **switching**: `mngr stop` the old agent (the chat app reaps its session's live state, codex's app-server connection included), then rename and archive it (4.3), record its final event count, and create the new agent (5.8).
   The queue was already returned in draining (5.3) and the open tk steps carry over (5.9), so nothing else is captured or closed here.
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
  the send itself fails;
  the request's turn ended with no file written, read off the activity tracker as an idle reading after a busy one or after a ten second grace (a turn shorter than one poll);
  a hard timeout of five minutes elapses (a summary can take a while).
  An API error, an agent out of tokens, or one wedged with a full context window all end the turn within seconds, so the idle rule catches them and the switch is immediate; no separate error detector is needed.
- No addendum is requested for user turns that land after the summary is written: only a direct `mngr message` bypass can add one, since the chat app holds its own sends, and the successor has the transcript and the `AGENTS.md` backstop for the rest.
- A stopped active agent is started for the request, by the same send path a message takes.
  Only when it cannot be started at all does the handoff proceed without a summary.
- The request renders as a chip ("Asked for a handoff summary") rather than a hidden line, so the pause reads as one; since phase 8 the page folds it into the handoff node.
- Whether a summary was produced, reused, or missing is recorded on the record's `handoff` entry and told to the new agent.
- Since phase 8, a retiring agent that never received a genuine user turn (`has_user_turn`) is not asked at all: the outcome is `skipped`, no prompt is built, and the confirming message (if any) reaches the successor as an ordinary held send. There is no context to carry, so the successor starts as a new chat would.

### 5.6 Cancel

Cancel is accepted in `draining` and `summarizing`: the record's `handoff` is cleared, the trigger message returns to the composer (it was typed for the new lane, so it is not delivered to the agent the user chose to leave), every other held send is delivered to the old agent in order, and the summary turn, if one is running, finishes as an ordinary turn.
The queued messages already returned to the composer stay there; the user resends what they still want.
The pending lane is kept in the UI so the user can try again.
Cancel is refused with 409 in `switching` and later.

### 5.7 Held sends and the trigger message

- From `draining` until the new agent is active, every send to the chat (from the composer, from a worker through the script, from anything else) is held: persisted on the record's `handoff` entry and answered 202 with `{"status": "held", "phase": ...}`, so the page keeps its "Sending" placeholder (shown with the phase text) and the script, which treats any 2xx as delivered-or-queued, never backs off around the hold.
  The held sends are delivered in order to the new agent right after the handoff prompt.
  This satisfies the conservation contract: the message is continuously visible and the backend, not the frontend, resolves the placeholder, which happens when the turn appears in the new agent's segment on the chat-keyed stream (4.7).
- A repeated `message_id` while held answers 202 like the first time and holds nothing twice: the delivered-id ledger phase 1 deferred, scoped to held sends.
- Sends keep being held in the `failed` phase too; they deliver on the retry.
- The trigger message is the first of the held sends.
  It rides the new agent's `mngr create --message-file` together with the handoff prompt (5.8); the others follow through the normal send path once the agent is ready.
- Delivery pops one held send at a time under the same lock the message route appends under, and the `handoff` entry is cleared only once the list is empty, so a send that arrives during delivery is delivered by the same loop rather than overtaking one still held.
- A cancel returns the trigger message to the composer and delivers the other held sends to the old agent (5.6).
- Held sends survive a chat-app restart because they are persisted on the record's `handoff` entry.

### 5.8 Creating the new agent

- The new agent id is minted and recorded on the record's `handoff` entry before the create runs, and passed with `mngr create --id`, so a resume can tell whether the create landed.
- The create is the existing chat create command: the reused mngr name, `--type <harness>`, `--template chat`, the account binding args from `binding.create_args`, `--label user_created=true`, `--label display_name=<title>`, `--label project=...`, `--label account=...`, plus `--label chat_id=<chat id>`, `--label chat_seq=<n>`, `--env MINDS_CHAT_ID=<chat id>`, and `--message <handoff prompt>`.
  `--message` delivers after the harness signals readiness, the path `/welcome` already takes.
  The `--type` and the binding args are always explicit, never left to config: `.mngr/settings.local.toml` supplies the workspace's *default* account to a create that names none, and the handoff's target is usually not the default; CLI list flags append after the file's, so an explicit `--type` and `--env` win for the same variable or path.
  `require_create_account.py` gates every in-workspace create, this one included, on the local file naming a type: a handoff on a workspace whose last account was just removed is refused by the gate rather than by the chat app, and that verdict is what the failed-next-agent state shows (5.10).
- The handoff prompt is built once, stored on the `handoff` entry, and delivered verbatim by whichever attempt lands the successor, whatever lane the retry uses.
  It contains: the summary's text, with the path it is kept at, or the statement that the predecessor did not produce one (the text travels inside the prompt so the successor has its context before its first tool call); the predecessor's archived mngr name, agent id, and state dir, plus the same for every earlier member, so `mngr transcript` and the find-transcripts skill reach them; a note that the open steps carry over (5.9); the lanes involved; and then the user's message.
  Its text is a reference document in this template (`.agents/shared/references/continue-chat.md`) that the chat app fills in, so it stays harness-neutral and editable.
- Since phase 8 the create is silent (no `--message`): the successor is tracked the moment its create returns, the model the user picked in the dialog is applied through the harness's own switch path (validated against the successor's option set exactly as the model bar's pick is), the successor is then appended to the record and the chip emitted, and only then does the prompt go to it through the send path, followed by the held sends. The record notes the prompt's delivery so a resume does not repeat it.
  This replaces principle 24's reset to the harness default with "the user's pick, else the default", and is what lets the pick govern the successor's whole segment.
  A pick the successor cannot take (an unknown model, a daemon that refuses) fails the switch at that step (`failed_step: model`): the chat still lists its retiring agent, the page reads "Could not set the model on Codex", and a retry on the same account adopts the successor under its pre-minted id and reruns only the pick and the deliveries. A retry on another account destroys that successor first and creates afresh under the same id; one on another harness drops the pick, which named a model of the harness it was made for.
- The first-chat claim is never taken for a successor: the claim is only attempted for a create with an empty message, and a handoff always passes one, so the `first` template and `/welcome` stay with the workspace's first chat.

### 5.9 tk steps, workers, subagents

- Open step records carry over untouched (principle 12): tk scopes them by the chat id, so the successor sees them as its own, continues the ones that still apply, and closes the rest, as the prompt and `AGENTS.md` tell it to.
  The chat app closes nothing at handoff.
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
- `switching` resumes by checking, in order: is the old agent stopped (else stop it); does it carry the archival name (else the one rename that also sets its labels); is `final_event_count` recorded; does an agent with the pre-minted id exist.
- If an agent with the pre-minted id exists and is running or stopped, it is adopted as the new active agent and the handoff completes.
  If the create left something mngr refuses to reuse (`mngr create --id` raises `DuplicateAgentIdOnHostError` for an id present on the host), the partial agent is destroyed and the create rerun under the same id.
- `failed` resumes as failed.

Reconciliation is chosen over replay because every step is idempotent when checked against mngr's own state, and because a replayed step could double-send the summary request or double-archive.

### 5.12 What the model bar shows

A harness handoff resets model, effort, and fast mode to the new harness's defaults, since those live in the agent's own settings; no translation between harness catalogs is attempted.

## 6. Rebind

A rebind is the same gesture applied to an account on the active agent's own harness and lane (5.2): the agent stays the chat's, and restarts on the new account.

- Sequence (`chat_rebinds.py`), on the record's `rebind` entry: `draining` returns the queue to the composer through the stop button's interrupt (the bounded wait for an in-flight send included), on the request's thread as a handoff's does; `restarting` then runs on a thread of the chat app's: `mngr stop` the agent, rewrite the binding in its own state dir, drop its resident watcher (only once the new binding is on disk, so a read that rebuilt it meanwhile is dropped too), `mngr label <agent> --label account=<new id>`, `mngr start <agent> --no-resume`, then deliver the held sends (the confirming message first) through the normal send path and clear the entry.
  The binding rewrite is `binding.rebind_agent`: the `CLAUDE_CONFIG_DIR=` line of the state dir's `env` file for claude (mngr sources that file into the tmux session on every start and never rewrites it), the credential link in the state dir for codex, pi, and antigravity (`account_scope`'s tables, the same link `create_args` repoints at create); each write lands whole, by a temp file or link renamed into place.
  No mngr command is added for the edit; the env file is the interface (principle 27).
- Claude keeps a chat's session files under the config dir it runs with, at `<account>/projects/<encoded work dir>/<session id>.jsonl` with the subagents under `<session id>/` beside it, and both mngr's resume chain and the chat app's watcher look them up under the current config dir.
  So before the env line changes, the rebind moves the agent's sessions (the ids in the state dir's `claude_session_id_history`, plus the agent's own uuid) from the previous account's tree to the new account's, keeping their place under `projects/` (`harnesses/claude/session_files.py`), and tracks the dir the files are under on the `rebind` entry (`claude_sessions_config_dir`: the previous config dir, recorded before the rewrite, then the target once they moved), so a resume after the rewrite, or a retry on a third account after a failed start, still knows where the files are and moves them on.
  The other harnesses keep sessions in the agent's state dir, so only the credential moves.
- The transcript, the tk steps, and the model settings carry over: the transcript because the watcher is rebuilt against the same files under the new dir, the model settings because every harness keeps its launch settings per agent (claude's in mngr's managed settings overlay in the state dir).
- The trigger message and anything sent meanwhile are held on the record and delivered once the agent is up, shown with the same phased placeholder; mngr's own send waits for a claude or antigravity TUI to be ready before it pastes, pi's send is an inbox append its extension drains once running, and codex's session retries while its daemon comes up, so the first send after the restart lands on every harness.
- The confirm dialog's rebind variant is one line ("Claude restarts on Anthropic 2 (Claude Code) and keeps this conversation."), the phase text reads "Restarting Claude on Anthropic 2 (Claude Code)...", and the 409s name the account.
- There is no cancel: draining runs on the request's thread and the stop follows at once, so the confirm is the last chance; the cancel route answers 409 with a detail that says so, and the page never shows "Cancel switch" for a rebind.
- A start that fails leaves the chat in the `failed` phase with mngr's output, the binding and the label already at the new account; the page's retry offers the accounts of the same harness and lane (the agent stays the chat's) and reruns the restart, every step finding its work done; "Start a new chat instead" is the way out to anywhere else, and destroy remains available.
- Feasibility per harness: `REBIND_VERIFIED_HARNESSES` lists every harness with an account scope (claude, codex, pi, antigravity), and a same-lane target on a harness outside the list falls back to a handoff, which is the spec's fallback for a harness that cannot resume under a swapped credential; the hand test with two real accounts per harness is the user's, and a harness that fails it is dropped from the list.
- Every step is idempotent and re-checked on resume (`_resume_handoffs` spawns the rebind runner beside the handoff runner): a stopped agent is not stopped again, a moved session file is not moved again, the env line and the link are rewritten to the same value, `mngr label` merges, and `mngr start` is a no-op for a running agent.
  The entry records the account the start landed on (`restarted_account_id`, written in the same update that moves the active entry), so a resume that finds it naming the target has only the delivery left; the active entry cannot serve as that marker, since it names the previous account until the start lands and a retry may name the previous account as its target.
- A rebind on a chat of one agent gets its first record like a first handoff does, and the record is dropped again when the rebind completes; on a multi-agent chat the active entry's `account_id` and `lane` are updated in place.
  The target becomes the most recently used account, as a launch does.

## 7. The template's side: AGENTS.md and the summary skill

- `AGENTS.md` gains a section every agent reads ("Continuing a chat that moved to you"): when `MINDS_CHAT_ID` is set and differs from `MNGR_AGENT_ID`, the agent is a successor in an existing chat; it reads the summary its first message names, or gathers its own context by listing its predecessors (`mngr list --include 'labels.chat_id == "$MINDS_CHAT_ID"'`) and reading their transcripts (`mngr transcript`, or the find-transcripts skill); it checks `tk steps`; it never touches a predecessor; and it tells the user none of this unless asked.
  This is the backstop for every failure mode of the summary, and it costs nothing when the summary exists.
- A `handoff-summary` skill for the retiring side: the slash command the chat app sends, naming the output path; the skill writes the summary and stops, without replying to the user.
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
- The worker path (4.5): `create_worker.py` stamps `lead_agent` with the lead's agent id, `worker-reporting.md` makes the same-repo write the primary delivery (phase 2 replaces the id-addressed rsync for a lead in a worktree with the stamped `lead_work_dir`), and `transcript-exploration.md` reads the lead's transcript by that id.
- Tests: the script against a stub chat app (delivered, blocked, refused, 503 then delivered, unreachable, and 404); the route's 503 gate in the chat app's suite; `create_worker_test.py` asserting the stamped id; one integration test under the vendored mngr that renames a local agent and shows `mngr transcript` still resolves it by id.
- Exit check: a worker's report reaches a lead that was renamed mid-task, its `mngr transcript $LEAD_AGENT` still reads, and a lead's reply reaches the worker through the chat app.

### Phase 2: the rename

- `ChatId` in a new `imbue/chat/primitives.py`, distinct from `AgentId`; a bridging rule that a chat's id equals its first agent's id, marked `CLEANUP` where it is assumed.
- Chat-level names on instance records, provisional records, the display name, the project label, presence, message stamps, pending permissions, and the OOM prioritizer (4.8).
- `AgentManager`'s chat-level duties (naming, provisional chats, snapshots, nudges, status) and agent-level duties (observe, mngr commands, trackers, sessions, model watchers) named apart and grouped; a physical split into two classes is allowed where it is mechanical but not required in this phase.
  `ChatState` renamed `ChatAppState`.
- The `ChatSnapshot` wire shape with `active_agent` (4.6), the renamed WebSocket messages, the chat-keyed routes with the agent-keyed aliases.
- The frontend: `agentId` becomes `chatId` in every model and view, `AgentManager.ts` becomes the chats model, `ProtoAgent` becomes a provisional chat, and every agent-level read comes from `active_agent`.
- `MINDS_CHAT_ID` on every chat-app-created agent; `layout.py`, the automation prompt, and the skills that name their own chat prefer it with the `MNGR_AGENT_ID` fallback; the client-activity key becomes the chat id.
  `create_worker.py`'s `lead_agent` stamp stays the dispatching agent's id (4.5) and gains `lead_work_dir`, which replaces the `mngr rsync` report delivery.
- The auto-open reactor's ledger and address (4.8) keyed by chat id.
- Permission requests carry the chat id in their `agent_id` field (4.5); no minds-side change in this phase, since every chat's id is its live agent's until phase 3, and the resolution nudge's move to the chat lands with phase 4's paired minds branch.
- Subagent keys become `<chat_id>.<agent_id>.<session_id>`.
- The transcript loader lifted out of each watcher, and the segment facade in front of it with exactly one segment.
- The handoff phase enum declared and carried as `null`.
- Contracts section 4.3, the meta spec's section 7.4, the chat README, and the paired minds glossary entry updated to the new vocabulary.
- The only externally visible differences: the new env variable, the new subagent key shape, the renamed WebSocket messages (the pages are served by the same process, so no cross-version skew), and the new routes beside the aliases.
- Exit check: every existing test passes under the new names, the minds e2e suites pass unchanged against the aliases, and no behavior differs.

### Phase 3: a chat can have several agents, read side

- The chat record store (4.2): `chat_records.py`, a `ChatRecord` of `ChatAgentEntry`s plus an optional handoff entry, behind a `ChatRecordStore` interface with a file implementation (`data/.apps/chat/chats/<chat_id>/record.json`, atomic writes under a per-chat lock, the version guard) and an in-memory one for tests.
  The manager reads every record at build (`refresh_chat_records` re-reads) and resolves every chat-versus-agent crossing through them: an agent a record names belongs to that chat, every other agent is a chat of its own (the own-chat rule).
  A record this build cannot read costs its chat the record (its agents fall under the own-chat rule) and nothing else.
- Membership from the record: one instance per chat, keyed by its first agent, listed from its active agent; an archived member is excluded because the record names it, never because of an `archived_at` label; a record whose active agent mngr does not list lists nothing.
  The `chat_id` and `chat_seq` labels are not consulted by the chat app in this phase (the record is the truth); they remain for `mngr list --include` from agent-side scripts.
- Status, stop, start, rename, and the pending-permission read come from the active agent; destroy is one `mngr destroy --force` naming every member by id, after which the record is deleted with its agents (the one record write this phase makes, as part of the destroy verb); the re-auth restart skips archived members (4.4).
  An archived member leaving the agent list (the observe stream's removal) leaves its chat's per-chat state standing; the active agent stopping drops the whole chat's resident transcripts, archived segments included.
- Multi-segment transcript reads (4.7): each harness's watcher is split into a `TranscriptLoader` (the read side: discovery, incremental reads, payload re-parsing) and the watcher over it (the watch loop, the queue feed, the model-bar write), registered as `HarnessSpec.loader_class` beside `watcher_class`; an archived segment is read through its loader, built on the first read that reaches into it and cached on the app state beside the watchers.
  `ChatTranscript` (`chat_transcript.py`) reads across the segments: chat-global totals and offsets from the recorded counts (the parsed count wins once a segment is loaded, with a warning on disagreement), the tail, backfill, forward, and offset reads crossing segment boundaries, the detail read resolved by event id, and every event stamped with its `agent_id` at ingest (the live stream included).
  Loading is lazy and sequential: no thread pool, since a segment loads only when a read reaches back that far and the goal is to keep archived history out of memory.
- The switch chip is the chat-level `agent_switch` event, synthesized between two segments with an id derived from the chat id and the retiring agent's sequence number; it carries `from_agent_id`, `to_agent_id`, `from_harness`, `to_harness`, and `seq`, plus `message_id` and `message` (the message the user switched with, kept on the successor's record entry as `opening_message` when the handoff folded it into the prompt; null for a fresh start, whose message is delivered as a turn of its own), counts toward the chat's total, and renders as a centered chip that opens the new agent's first section, on that message as the user's bubble when there is one.
- The subagent routes resolve any member of the chat, archived ones through their loaders; the page reads a subagent view's key off the `/_instances` create response.
- Codex's few position-derived fallback event ids (a message, call, result, or turn marker codex gave no id) became content-derived, so two codex agents of one chat cannot mint one id from one line number; a repeat across segments is logged, and the earlier segment's event wins.
- Nothing writes a record except tests and the destroy verb's delete.
- Exit check (met in `agent_manager_test.py`, `server_test.py`, `instances_test.py`, `chat_transcript_test.py`): a hand-built two-member record renders one continuous transcript with correct offsets, paging, jumps, detail fetches, and per-segment subagent views; stop, start, destroy, and rename act on the right agents.

### Phase 4: the handoff, backend

- The handoff (`chat_handoffs.py`, a runner the manager hands bound callables to, the shape the harness sessions take): the switch route and its preconditions (5.2), draining as the stop button's interrupt (5.3), the four phases on the record's `handoff` entry (5.4), the summary request with reuse of a fresh summary and the three proceed conditions (5.5), cancel (5.6), held sends answered 202 and delivered under the record lock (5.7), the successor's create under its pre-minted id with the stored prompt through `--message-file` (5.8), the failed phase with a retry route (5.10), and resumption from any phase on the manager's start (5.11).
  The archive is one `mngr rename` carrying every label (4.3); the addendum and the closing of tk steps were dropped (5.5, 5.9).
- While a chat converges it keeps listing and reading from its retiring agent (the record's last entry stands in for the active agent), with status `working`, or `error` once the create failed; its title and name come from the handoff entry, since the stand-in may already carry its archival name.
  Stop, start, rename, interrupt, the queue actions, and the model change answer 409 with the phase (the instances API's stop, start, and rename too); destroy proceeds, and a send is held.
- The transcript fan-out and the SSE streams are keyed by chat id rather than agent id, so an open page's stream follows the chat through the switch and carries the `agent_switch` chip the moment the successor is active.
- `HandoffState` on the wire gains `error`; the summary request renders as a chip.
- tk's creator scoping moved to the chat id (principle 12, 5.9), in the vendored `tk` script and the reminder hooks.
- The `handoff-summary` skill, the `continue-chat` reference, and the `AGENTS.md` section (section 7).
- Verified by reading: the minds desktop client registers every newly discovered agent with latchkey, so a successor needs no registration step; the archival rename follows a stop, so tmux never sees the name.
  `DuplicateAgentIdOnHostError` on a resumed create destroys the half-made agent and reruns the create once.
- The paired minds branch routes the permission-resolution nudge through the chat app by chat id (4.5).
- Tests: `chat_handoffs_test.py` drives the runner through every phase against a recording `mngr` (the full switch, a reused summary, a turn ending without one, a busy agent, a refused request, a failed create and its retry, a half-made successor, a cancel mid-summary, a resume mid-switch); `agent_manager_test.py` and `server_test.py` cover the verbs, the 202 hold, the 409 refusals, the cancel, and a chat converging end to end over the fake binary; the two-account e2e is a release test that skips until both accounts are signed in.
- Exit check: a handoff driven through the API completes against the recording `mngr`, resumes from a record left in each phase, fails over to the failed state when the create is made to fail, and retries.

### Phase 5: the handoff, UI

- The user's side of the handoff as section 5.1 records it: the pending lane on the provider menu (cross-harness accounts only; same-harness accounts keep the new-chat prompt until phase 6), "Switch and send" with the two-line confirm, the held messages rendered from the snapshot with the phase as their caption, the activity strip's phase text, "Cancel switch" in place of Stop, the failed page with a retry on any signed-in account, and the model bar following the new agent with any optimistic pick forgotten.
- The wire (4.6): `handoff` gained `target_harness` and `held_sends`; the record gained `trigger_text`.
  The 409 every refused verb answers names the target harness and the phase in plain words (4.4), and the refused controls show it as is rather than being greyed out first.
- Tests: vitest for the pending lane, the composer's switch, cancel, and placeholder, the held bubbles and their bookkeeping across the switch's end, the activity strip, the failed notice, and the active-agent listener; and three Playwright release tests in `test_e2e.py` that drive the whole flow through the browser against the recording `mngr` (the switch, a cancel during summarizing, and a forced create failure retried on a third account).
- Exit check, as met: the browser flow runs deterministically against the recording `mngr` in the e2e suite; the two-account release test (`test_handoff_release.py`) stays API-driven until CI has an OpenRouter key.

### Phase 6: rebind

- The rebind as section 6 records it: `chat_rebinds.py` (the runner, its phases `draining` and `restarting` on the record's `rebind` entry, the failed phase and its same-lane retry), `binding.rebind_agent` and `REBIND_VERIFIED_HARNESSES`, the claude session-file move, the dispatch by harness and lane in the switch route (5.2), and the wire's `kind` and `target_label` (4.6).
- The page: every account but the chat's own sets the pending lane (the "Launch a new chat?" prompt is retired), the confirm's rebind variant, "Start a new chat instead" on the confirm and on the failed page (a new chat on the account, with the typed message from the confirm), the rebind's phase text, and no "Cancel switch" for a rebind.
- Tests: `chat_rebinds_test.py` drives the runner through every phase against a recording `mngr` (the claude move, a symlink harness, a failed start and its retry, a refused relabel, a resume after the env was rewritten, a gone agent or account, a refused held send); `agent_manager_test.py` and `server_test.py` cover the dispatch, the 202, the 409s, the cancel refusal, and the retry rule; vitest covers the lane rule, the confirm, the new-chat action, the phase text, and the failed page; one Playwright release test in `test_e2e.py` drives a rebind through the browser against the recording `mngr`.
- Exit check, as met: the browser flow runs deterministically against the recording `mngr`; the per-harness hand test with two real accounts is the user's, with `REBIND_VERIFIED_HARNESSES` as the switch for any harness that fails it.

### Phase 7: cleanup

- The `/api/agents/...` aliases are gone: the per-chat routes, `/api/chats/create`, and the subagent reads answer under `/api/chats` alone, `CreateAgentResponse` went with the create alias, and `GET /api/agents` stays as the plain listing (principle 29).
  `system/scripts/message_chat.py` posts to `/api/chats/<chat_id>/message`; its 404 backoff to `mngr message` is unchanged, and still right for a chat app from before the rename, which has no `/api/chats` routes and no handoffs either.
- One auto-name word, "Chat" (4.3): `naming.py`'s `AUTO_NAME_WORD` replaces the per-harness table and `lanes.py`'s per-lane overrides.
- The browser fleet addresses the chat that holds a browser (principle 31): the fleet CLI's owner id is `MINDS_CHAT_ID`, else `MNGR_AGENT_ID`, and the daemon's wake-ups and nudges reach that chat through the messenger.
- The paired minds branch retargets the minds_evals bridge (`/api/chats/create` answering `chat_id`, `/api/chats/<chat_id>/message`, `/events`, and `/model`) and the deployment test that creates a chat; the e2e runner keys on the URL shape and is unchanged. It merges after this template is tagged (principle 32).
- Docs: the chat README, the workspace app model's section 7.4, its mngr-side note, the oom_priority README's presence route, the minds glossary and the minds_evals README; the per-phase specs and older plans keep their historical route names.
- Not removed, on purpose: the `${MINDS_CHAT_ID:-$MNGR_AGENT_ID}` fallbacks across the skills and `layout.py` (they serve agents created outside the chat app, which never get the variable), and the `CLEANUP` marks of other arcs.

## 9. External callers and compatibility

| Caller | Today | After phase 2 | After phase 7 |
|---|---|---|---|
| minds e2e runner (`e2e_workspace_runner.py`) | finds the chat frame by `/agent-<hex>/` | unchanged (chat ids keep the prefix) | unchanged |
| minds_evals bridge (`minds_bridge.py`) | `/api/agents/create-chat`, `/api/agents/<id>/message`, `/events`, `/model` | served by the aliases | `/api/chats/create` (answering `chat_id`), `/api/chats/<chat_id>/...`; the `/api/agents` listing as before |
| minds deployment tests (`test_litellm_via_workspace.py`) | `/api/agents/create-chat` | the alias | `/api/chats/create` |
| minds assist and update-self chats | a bare `mngr create --template chat` inside the workspace, bound to the default account and harness through `.mngr/settings.local.toml`, carrying `account=<default>` | own chats, no `MINDS_CHAT_ID` | unchanged |
| the `automation` template prompt | `app:chat?instance=$MNGR_AGENT_ID` | `$MINDS_CHAT_ID` with fallback | unchanged |
| the shell | instance keys, addresses, `/api/client-activity` keys | chat ids, which equal today's keys | unchanged |
| the minds chrome's permission routing | request `agent_id` = chat frame URL | chat id | unchanged |
| the minds latchkey handlers' resolution nudge (`mngr message <agent_id>`) | the request's `agent_id` | unchanged (the chat id is the live agent's id until phase 3); phase 4's paired minds branch routes the nudge through the chat app by chat id (4.5) | unchanged |
| the browser fleet's owner id and wake-ups (`fleet.py`, `session.py`) | `MNGR_AGENT_ID`, messaged with `mngr message` | the messenger, still keyed by the claiming agent's id | `MINDS_CHAT_ID`, else `MNGR_AGENT_ID` (principle 31) |

## 10. Open questions and things to verify

- Settled for phase 4 (4.3): the archival rename follows `mngr stop`, so tmux never carries the archival session name; only an archived agent started by hand would test it.
- Settled for phase 4: the minds desktop client's latchkey auto-registration follows every newly discovered agent, so a successor needs no registration step of the handoff's.
- Settled for phase 2 (4.5): the minds side treats a permission request's `agent_id` purely as a routing key (the gateway checks its shape, the chrome routes by workspace and matches the card by `request_id`), so the request carries the chat id alone; the resolution nudge moves to the chat in phase 4.
- Settled for phase 6 (section 6): claude's session files move with the agent between account folders, so `claude --resume` and the watcher find them; the other harnesses keep sessions per agent. Whether every harness then resumes cleanly under the swapped credential is verified by hand per harness, and `REBIND_VERIFIED_HARNESSES` drops any that does not.
- Settled for phase 2 (4.5): the report is written into the lead's checkout, `lead_work_dir` when the launcher stamped it and the repo's main worktree otherwise; the id-addressed `mngr rsync` delivery is gone.
- Settled for phase 4 (section 7): the `AGENTS.md` section and the summary skill's content rules, as landed.
- Enabling the two-account e2e in CI: it needs an OpenRouter key beside the Anthropic one in the CI secrets.
  Phase 5's browser exit check runs against the recording `mngr` instead; the real two-account test stays API-driven until the key exists.

## 11. Documents this plan revises

- `docs/system/blueprint/workspace-app-model/contracts.md` section 4.3: the chat row's key, title, status, create, delete, stop, and start columns.
- `docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md` section 7.4.
- `system/apps/chat/README.md`.
- The minds repo's `apps/minds/docs/workspace/glossary.md` "chat agent" entry (rewritten for the local-settings account model since this plan was drafted, but still "one per chat tab"), and the mngr-side changes note in `docs/system/blueprint/workspace-app-model/mngr_side_changes.md`.
- `system/apps/chat/README.md`'s provider-accounts section, which now also describes `.mngr/settings.local.toml` and the auto-open reactor; both paragraphs are agent-keyed today.
