The workspace now has a **main chat**, where a message with no chat of its own goes. The chat the Mind app seeds is the main chat to begin with, and is now named "Main" instead of "Welcome"; that name is kept rather than replaced by one drawn from the first message, until you rename it:

- The chat list always leads with it, alone above a thin rule and in a medium weight, instead of sorting it by recency. Helper chats it started sit right under the rule.

- The main chat never sleeps: whenever it is found stopped (stopped from elsewhere, by a routine, or shed by the memory guard), the chat app starts it again. Its menu offers no stop or restart.

- With no chat selected, the chat list opens on it rather than on the most recently messaged chat; deleting the chat on screen also returns you to it.

- Pressing Enter in the desktop's launcher sends the text to the main chat ("Send to main chat"). "New Chat" moves to the second row, so Cmd+Enter (Ctrl+Enter elsewhere) starts a new chat, and "Send to chat..." stays in the list without a key.

- A draft or message sent to the "current chat" from a window that shows none (the desktop's "Design your own...") lands in it.

- Any chat you started can be made the main chat from its right-click menu (and the phone layout's kebab): "Make main chat". It moves to the top in place of the old main chat.

- The main chat can be deleted like any chat. Another chat becomes the main chat right away (the one messaged most recently, never a helper chat an agent started) and moves to the top; with no chat left, the new chat the list opens becomes the main chat.

The main chat is kept as a pointer in `data/.apps/chat/main_chat.json`, not as a property of any chat. `chats_updated` pushes and `GET /api/chats` carry it as `main_chat_id`.

- A chat that is still waiting for its first message (the new workspace's Main, or the empty chat the list opens) can now be renamed, made the main chat, and deleted from the list; only a chat whose agent is being created offers none of these until it is up.
