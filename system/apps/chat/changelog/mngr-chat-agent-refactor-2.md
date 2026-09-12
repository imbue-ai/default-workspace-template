Phase 2 of the chat-agent split (`docs/system/blueprint/chat-agent-split/`): the chat app names a chat apart from the agent it runs on. Every chat still runs on exactly one agent (its id is that agent's id), so nothing behaves differently for the user; the code, the wire, and the pages are written against chats.

- `ChatId` (`primitives.py`) is distinct from an agent id in code, with the bridging rule (a chat's id is its first agent's id) named in one place and marked `CLEANUP` wherever it is assumed. Instance records, provisional chats, presence, message stamps, the auto-open ledger, and the OOM prioritizer key by chat id. `ChatState` is `ChatAppState`.

- The chat pages' socket sends `chats_updated` (a `ChatSnapshot` per chat: id, title, name, project, status, labels, the agent ids, the handoff phase carried as `null`, and the agent-level facts under `active_agent`), `provisional_chat_created`, and `provisional_chat_completed` keyed by `chat_id`, in place of `agents_updated` and the `proto_agent_*` messages.

- Routes live under `/api/chats`: `/api/chats` lists the chats, `/api/chats/create` takes `chat_id`, every per-chat route is `/api/chats/<chat-id>/...`, and a subagent view reads `/api/chats/<chat-id>/agents/<agent-id>/subagents/<session-id>/{events,stream}`. Every per-chat route, `/api/chats/create`, and the subagent reads are also served at their older `/api/agents/...` spelling, whose id is read as a chat id; `/api/agents` stays the plain listing of every mngr agent. A subagent route whose agent is not the chat's answers `Chat '<chat-id>' has no agent '<agent-id>'` (404). The chat document keeps the workspace's `system-interface-agent-id` meta tag for plugins; the page itself reads its chat id from `system-interface-chat-id`. A subagent view's instance key is `<chat-id>.<agent-id>.<session-id>`, and its page carries the chat, agent, and session ids in meta tags.

- Every agent the app creates gets `--env MINDS_CHAT_ID=<chat id>`.

- The frontend: `agentId` is `chatId` in every model and view, `models/Chats.ts` replaces `AgentManager.ts`, a `ProtoAgent` is a `ProvisionalChat`, and every agent-level read (harness, activity, model choice, queue, lifecycle state, account) comes from the chat's `active_agent`.

- The transcript read routes go through `chat_transcript.py`, a chat's transcript as its agents' segments in order (one segment today); `StoreBackedWatcher` is split into the loader that reads the resident store and the watcher that tails the files, behind a `TranscriptReader` interface.
