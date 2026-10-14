Agent Memory has memory settings, modelled on the Claude apps': "Use memory" pauses memory for every chat, and a switch per kind of chat (Claude, pi) turns it off for just those. While it is off, chats keep what is saved but neither use it nor save anything new; the notes are not deleted. The settings say plainly that a chat still knows what it was told earlier in the same conversation, since pausing memory can't take that back.

- The first fact at the top says which chats use the notes ("Shared with your Claude and pi chats", "Used by your Claude chats only", "Memory paused"), and "Where your notes go" says who reads them.

- The settings say when a change reaches each chat: pi chats from their next message, new Claude chats right away, and open Claude chats are told on their next message; a Claude chat started while memory was off picks it back up only once restarted.

- The switches are kept in `data/.apps/memories/settings.json`. Turning Claude's memory off also sets `autoMemoryEnabled: false` in `.claude/settings.local.json` (keeping the file's other settings), which stops a new Claude chat from loading or saving memory. A settings file that can't be read is shown on the page and treated as off.

- The page and the chats read the settings file by one rule: keys and harnesses this version doesn't know are ignored, and a file that isn't valid (including a bad encoding) counts as off. Screen readers get each switch's name without its hint, and the unreadable-settings note meets WCAG AA contrast.
