Agent Memory has memory settings, modelled on the Claude apps': "Use memory" pauses memory for every chat, and a switch per kind of chat (Claude, pi) turns it off for just those. While it is off, chats keep what is saved but neither use it nor save anything new; the notes are not deleted. The settings say plainly that a chat still knows what it was told earlier in the same conversation, since pausing memory can't take that back.

- The first fact at the top says which chats use the notes ("Shared with your Claude and pi chats", "Used by your Claude chats only", "Memory paused"), and "Where your notes go" says who reads them.

- The settings say when a change reaches each chat: pi chats from their next message, new Claude chats right away, and open Claude chats are told on their next message (they pick memory back up once restarted).

- The switches are kept in `data/.apps/memories/settings.json`. Turning Claude's memory off also sets `autoMemoryEnabled: false` in `.claude/settings.local.json` (keeping the file's other settings), which stops a new Claude chat from loading or saving memory. A settings file that can't be read is shown on the page and treated as off.

- The page loads faster with a long chat history: it searches transcripts as bytes and decodes only the lines that name the notes folder, about 1.7x faster on 350MB of transcripts, with memory bounded to one 4MB chunk at a time.

- Fixes from an independent review:
  - Deleting or editing a note is recorded for open chats before its index line is updated, so a failure updating `MEMORY.md` no longer leaves chats untold.
  - Index rewrites take a lock shared with the chats' index sync, so a racing sync can't write a deleted note's line back.
  - Notes with any visible Markdown file name (spaces, accents, a leading underscore) are listed instead of hidden.
  - A multi-line YAML summary no longer shows as `>` or turns invalid when edited.
  - A transcript time without a timezone no longer breaks the page.
  - The page and the chats read the settings file by one rule: unknown keys and harnesses are ignored, and a malformed file counts as off.
  - Rewrites keep a file's permissions.
  - The delete dialog and technical details no longer claim nothing keeps a copy: chats that read a note keep it in their transcripts, and the change record keeps the file name.
  - One edit at a time; a draft whose note a chat deletes stays on screen to copy.
  - Save is enabled only after a change.
  - Out-of-order refreshes are ignored.
  - Network and startup errors read in plain words.
  - Two texts were raised to WCAG AA contrast.
  - Screen readers get switch names without their hints, and an announced status.
