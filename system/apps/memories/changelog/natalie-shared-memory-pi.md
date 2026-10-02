"What agents know" now covers pi chats as well as Claude chats, since both keep the same notes.

- The page says Claude and pi chats share the notes (Codex, OpenCode and Antigravity don't yet), and credits a note a pi chat saved to that chat, from pi's own transcripts.

- Deleting or editing a note now tells the chats that are already open, which still have the note in their conversation and could otherwise write it back or undo the edit. Each change is recorded in `data/.apps/memories/user-changes.jsonl` by file name and time only, never what the note said, and kept 30 days.

- An edit or delete is recorded for open chats before the note's index line is updated, under a lock shared with the chats' index sync; if the index update fails, the next sync repairs it. pi transcripts are searched as bytes, like Claude's.
