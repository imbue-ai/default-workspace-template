# agent_memory

The agent's memory: the notes in `data/memories/` (the folder Claude's
`autoMemoryDirectory` points at), and the record of every change an agent made to
them, with undo.

## Modules

- `notes` -- reading and writing the notes and their index (`MEMORY.md`): the
  `NoteStore`, which keeps the index in step with the files and refuses a write over
  a note that changed underneath. Needs pydantic and PyYAML.
- `changes` -- the change log, stdlib-only (beyond the stdlib it imports only the
  stdlib-only `chat_background_tasks`), and the library's error base
  (`AgentMemoryError`). It is the only module bare code may import:
  `system/entry_points_testing.py` lists `src/` as a bare search path for it alone,
  since `notes` needs the venv. Claude's hooks record, per chat session
  and turn, ONE change record per note (its text before the turn's first change and
  after its latest, and the tool calls that made it) under
  `data/.apps/chat/memory_changes/`. The index is split by line: a note's pointer line
  goes with that note's record, so a note and its index line undo together, and the
  index's other lines (headings, lines written straight into it) are the turn's one
  index-only record. `ChangeLog.undo` puts back only that record's part -- the note's
  file and its own index lines, every other index line kept as it stands -- refusing
  (unless forced) when any of it changed again since. Records written before the
  split hold whole files and undo whole.
  Records are typed (`MemoryChange`, `ChangedFile`, each with `to_json`/`from_json`).
  `ChangeLog` takes its record cap and idle cutoff as arguments; `default_log(environ)`
  builds the main checkout's with the defaults (the newest 1000 changes, and a session's
  bookkeeping, a copy of the folder, dropped once it has not started a turn in 30 days).

## Consumers

- `system/scripts/agent_memory_change_log.py` -- Claude's UserPromptSubmit and
  PostToolUse hooks, which `.claude/settings.json` wires: a stub that puts this
  package's and `chat_background_tasks`' sources on `sys.path` and runs
  `workspace_bare_scripts.agent_memory_change_log`, which imports only `changes`.
- The chat app -- shows each change as a card in the conversation, at the last tool
  call that touched it, with the changed lines and an Undo button
  (`imbue/chat/memory_changes_endpoints.py`).
- An app that views or edits the notes (a user-built Memory app, say) -- through `notes`,
  as an editable path dependency. When the workspace has an app registered as `memory`,
  the chat's card links each note to it (`?note=<id>`).
