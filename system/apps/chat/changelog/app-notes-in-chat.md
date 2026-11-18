Another app in the workspace can put a short one-line note into a chat, such as "Claude usage 2%" from AI Usage Limits.

- A note is not a message: no agent sees it and nothing replies to it. It is shown between the chat's messages at the moment it arrived, as a quiet one-line notice on the reply line.
- `POST /api/chats/{chat_id}/app-notes` adds one and is loopback-only, so only a process inside the workspace can; `GET` on the same path lists a chat's notes, oldest first.
- Notes are kept per chat as an append-only log (`data/.apps/chat/app-notes/<chat_id>.jsonl`), cut back to its newest notes once it grows past a bound, and sent live on the chat's stream as `app_note` frames. The page reads the log when it connects, so a note that arrived with no page open still shows, and a live note survives a reread.
