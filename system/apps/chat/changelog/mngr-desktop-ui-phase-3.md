The chat app now serves a chat root page at `/`: the chat list down the left (grouped by the chat that started each helper, ordered by recency, with status marks, an unread check for a finished turn you have not looked at, rename, stop and restart, and delete) beside the selected chat in an inner frame. The selection is the `chat` query parameter, so the root's path is `/?chat=<id>`, which it reports to the shell together with the chat's title; it handles `shell:navigate` by changing the selection, and keeps the last few chats' frames alive so switching back is instant.

`/new` is the chat's launch path: the root with a chat just created and selected (`account_id` and `message` params as before). With nothing signed in it opens the provider chooser first.

Every chat page now reports its path and title to the shell, and the chat's terminal back face attaches through the new `terminal-pty` origin when one is registered.

Chat snapshots carry `last_messaged_at`, and `POST /api/chats/<id>/rename` renames a chat on the chat app's own routes.
