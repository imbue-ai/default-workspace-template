pi chats now keep the workspace's shared memory (`data/memories/`) alongside Claude chats, in the same notes and format, so a fact one chat saves reaches the other.

- New `.pi/extensions/memory.ts` gives pi what Claude Code gives Claude. Before every pi turn it adds two system prompt sections: the memory protocol and the current `MEMORY.md` index, with any notice of notes the user deleted or edited. Neither depends on the clock, because pi appends a changed section's full text to the conversation; this way the protocol is recorded once per chat and the index only after a note changes. Right after pi writes or edits a note, the extension stamps the note's `metadata.modified` (and `metadata.source` when missing), so the date is never the model's guess. It fails open, logging to `$MNGR_AGENT_STATE_DIR/pi_workspace_memory.log`, and keeps working when `tk_workflow.ts` replaces the prompt.

- New `system/scripts/agent_memory_context.py` (standard library only, listed in `stdlib_only_scripts_test.py`) renders those sections, stamps notes, and backs a new UserPromptSubmit hook in `.claude/settings.json`. Before each Claude message the hook announces the user's deletes and edits, and notes other chats saved, that are newer than its last run for that chat. Claude Code loads the index only when a chat starts, so without it a Claude chat never saw a note a pi chat saved later, and could write a deleted note back from its conversation.

- pi's system prompt in `.mngr/settings.toml` no longer tells it to avoid memory.

- `CLAUDE.md` tells Claude chats never to save secrets, not to save sensitive personal details (health, race or ethnicity, religious beliefs, political views, sexual orientation or gender identity) unless asked, and that the user's deletes and edits win.

- `AGENTS.md` and `data/memories/README.md` describe the folder as shared by Claude and pi chats.
