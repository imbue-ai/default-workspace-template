The chat now offers suggestions: short replies above the composer that the user can click instead of typing.

- When a chat has suggested replies to its last message, they appear as a row of pills directly above the composer. Clicking one sends it as a message; with text already in the composer, that text is sent with the suggestion on a new line, along with any attachments. Once a message is sent, the row folds into a "Next steps" toggle that opens it again. A chat room shows none.

- A new workspace's first chat opens on a shorter welcome that ends in "How do you want to start?", over two suggestions: "Describe what you want to make" and "See what Studio can do". Once a provider is connected the agent no longer speaks first: it waits for a pick or a typed message, and answers a pick with a prepared reply (the second also opens the Getting Started app beside the chat). What an older Imbue Studio app says the user wants Studio for still reaches the agent.

- The workspace's owner can turn the suggestions off with the "disable suggestions" link beside them, with "Undo" offered for a few seconds. The chat settings gain two switches, both on by default: `is_next_step_suggestions_enabled` and `is_new_chat_suggestions_enabled`. `PUT /api/settings/suggestions` changes either or both and leaves every other setting as it is, and every settings change is pushed to the open pages as `settings_updated` on `/api/ws`.

- `GET /api/chats/<chat-id>/suggestions` answers the suggestions stored for a chat (`suggestions.json` in the chat's folder), or `none`.
