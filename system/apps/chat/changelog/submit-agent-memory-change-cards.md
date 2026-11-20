When the agent changes its memory, the chat shows a "Memory changed" card inside the reply, at the tool call that made the change: what changed in one line, the changed lines in plain text on request, and an Undo that takes back only that note and its index line.

- The claude parser turns the memory change-log hook's note (a `hook_additional_context` attachment) into a NOTICE `user_message` carrying `memory_change_id` and `tool_call_id`; it is never a turn awaiting a reply.
- New routes: `GET /api/chats/{chat_id}/memory-changes`, `GET /api/memory-changes/{change_id}`, and `POST /api/memory-changes/{change_id}/undo` (409 when a file changed again since, unless `is_forced`); the card then asks in place, "Undo anyway" or "Keep it".
- The card stands inside the reply without breaking its section, and only the last notice of a change draws it. When a workspace registers an app named `memory`, the card links to the note there.

When the Memory app is registered (it is a built-in now), the card's link opens it on the note (`/?note=<id>`).
