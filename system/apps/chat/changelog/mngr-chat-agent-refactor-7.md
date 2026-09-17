Phase 7 of the chat-agent split (`docs/system/blueprint/chat-agent-split/`): the cleanup.

- The older `/api/agents/...` spellings of the chat routes are gone. Every per-chat route, the create, and the subagent reads answer under `/api/chats` alone; `POST /api/chats/create` answers `chat_id` where the retired `POST /api/agents/create-chat` answered `agent_id`. `GET /api/agents` stays as the plain listing of every mngr agent.

- Every new chat is named "Chat N", whatever harness or lane it starts on: a chat can switch harness now, so a name that said "Codex 1" would be wrong the moment it moved. Chats named before this keep their names, and new numbers skip every taken name.
