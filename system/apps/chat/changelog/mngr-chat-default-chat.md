The workspace's welcome chat (the one the Mind app seeds) is now its **default chat**, where a message with no chat of its own goes:

- The chat list always leads with it (its helpers under it), in a medium weight and set off from the other chats by a thin rule, instead of sorting it by recency.

- With no chat selected, the chat list opens on it rather than on the most recently messaged chat; deleting the chat on screen also returns you to it.

- A draft or message sent to the "current chat" from a window that shows none (the desktop's "Design your own...") lands in it.

Chat snapshots and provisional chats carry a new `is_default` field. A workspace that was never seeded, or whose welcome chat was deleted, has no default chat and keeps the previous most-recent-chat behavior.
