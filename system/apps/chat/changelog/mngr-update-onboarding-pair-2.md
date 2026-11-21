The chat now works out its own suggested replies.

- When an agent finishes a reply, the chat suggests two or three replies to it a few seconds later, as the pills above the composer. One may be marked as the recommended one, in green. A longer set shows its first three with "Show more". Nothing is suggested when no reply would make sense, under a reply that is a provider failure, in a chat room, or in a chat an agent started.

- The suggestions come from one call per finished reply to a mid-sized model (Sonnet for Claude, the account's newest "terra" model for Codex) on the account the chat already runs on, sent the last few turns of the conversation. The call stays outside the chat's transcript. Every chat gets them, whether or not it is on screen; switching "Next step suggestions" off stops the calls. Chats on Pi, Antigravity and OpenCode get none.

- A chat's suggestions never outlive the reply they were made for: the stored set is replaced or removed each time a turn ends.

- `GET /api/chats/<chat-id>/suggestions` answers `pending` while a set is being worked out, and `/api/ws` pushes `suggestions_updated` with the `chat_id` when a chat's stored set is replaced or removed.
