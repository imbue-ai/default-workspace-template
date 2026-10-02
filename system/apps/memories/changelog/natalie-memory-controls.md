Agent Memory has memory settings, modelled on the Claude apps': "Use memory" pauses memory for every chat, and a switch per kind of chat (Claude, pi) turns it off for just those. While it is off, chats keep what is saved but neither use it nor save anything new; the notes are not deleted.

- The first fact at the top says which chats use the notes ("Shared with your Claude and pi chats", "Used by your Claude chats only", "Memory paused"), and "Where your notes go" says who reads them.

- The settings say when a change reaches each chat: pi chats from their next message, new Claude chats right away, and open Claude chats are told on their next message (they pick memory back up once restarted).

- The switches are kept in `data/.apps/memories/settings.json`. Turning Claude's memory off also sets `autoMemoryEnabled: false` in `.claude/settings.local.json` (keeping the file's other settings), which stops a new Claude chat from loading or saving memory. A settings file that can't be read is shown on the page and treated as off.
