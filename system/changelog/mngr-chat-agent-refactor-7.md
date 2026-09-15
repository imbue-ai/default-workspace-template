Phase 7 of the chat-agent split (`docs/system/blueprint/chat-agent-split/plan-chat-agent-split.md`): the cleanup.

- `system/scripts/message_chat.py` posts to the chat app's `/api/chats/<chat-id>/message` route now that the `/api/agents/...` aliases are gone; its backoff to `mngr message` when the chat app cannot take the message is unchanged.

- The spec records what phase 7 landed and the decisions it settled (section 3, items 28 to 32; the Phase 7 entry; section 9's table), the workspace app model's section 7.4 and mngr-side note name the dropped aliases, and the oom_priority README names the chat-keyed presence route.
