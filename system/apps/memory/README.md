# memory (the app)

Memory: the notes the agent remembers about the user across chats -- preferences,
feedback, projects and references -- to view and edit. Served on port 8070 by the
`memory-app` package; the design is `docs/system/specs/memory.md`.

- **Notes.** The navigation mirrors the index the agent loads every session
  (`MEMORY.md`): its headings, each note it points at, the lines written straight
  into it (saving one gives it a note of its own in its place), the lines that point
  at a missing file, and the notes it leaves out. The editor saves a note's title,
  kind, summary, text and its Why and How to apply; a note changed elsewhere since
  it was opened is refused rather than overwritten. A new title renames the note's
  file and every `[[link]]` to it; a title may not hold square brackets; an index
  line past the ~150 characters Claude Code suggests is flagged (it still saves).
  Each note and the index can be shown as the raw file.
- **The agent's changes.** Shown and undone on the chat's "Memory changed" cards,
  not here.

It keeps nothing of its own. The notes are `data/memories/` (Claude's
`autoMemoryDirectory`) and the changes are the records Claude's change-log hook
(`system/scripts/agent_memory_change_log.py`) writes under
`data/.apps/chat/memory_changes/`, both through the `agent_memory` library
(`system/libs/agent_memory/`), which the chat's cards use too. A save here moves
every chat's baseline past it, so it is never shown as a change the agent made.
The chat's cards link a note here as `/?note=<id>`, and the page reports the note
it shows the same way, so the shell reopens a window on it.

`AGENT_MEMORY_DIR` and `AGENT_MEMORY_CHANGES_DIR` point the app at other folders,
which a preview (`[preview]` in `app.toml`) sets to copies of both.
