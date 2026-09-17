Paired branch for phase 7 of the default-workspace-template's chat-agent split (`docs/system/blueprint/chat-agent-split/plan-chat-agent-split.md` there), which drops the chat app's `/api/agents/...` aliases.

- The bridge creates the driver's chat through `POST /api/chats/create` and reads the chat's id from the answer's `chat_id`; the sends, the event reads, and the model choice address `/api/chats/<chat_id>/...`. The plain `/api/agents` listing the bridge resolves a chat by name from is unchanged.

- Merge after the template is tagged: a workspace from before the split has no `/api/chats` routes.
