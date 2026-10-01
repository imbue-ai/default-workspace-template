pi chats keep the workspace's shared memory (`data/memories/`) alongside Claude chats.

- New `.pi/extensions/memory.ts` puts the memory protocol and the current `MEMORY.md` index in every pi turn's system prompt, as its own section (or appended, when another extension already replaced the prompt for that turn). It fails open.

- New `system/scripts/agent_memory_context.py` prints what that section holds: the protocol filled in with the notes folder, the harness and the current UTC time (Claude Code stamps a note's `modified` itself; a model left to guess the date gets it wrong), then the index. It is standard-library only, since pi runs it under the system `python3`, and listed in `stdlib_only_scripts_test.py`.

- pi's system prompt in `.mngr/settings.toml` no longer tells it to avoid memory; it points it at that section instead.

- `AGENTS.md` and `data/memories/README.md` describe the folder as shared by Claude and pi chats.

- Chats are told when the user deletes or edits a note in "What agents know". `agent_memory_context.py` reads the app's record of those changes and adds a notice to pi's memory section; with `--claude-hook`, run by a new UserPromptSubmit hook in `.claude/settings.json` before every Claude message, it prints that notice plus the notes other chats saved since the Claude chat started, which Claude Code's once-per-chat index load would otherwise miss. Without it, an open chat that still had a deleted note in its conversation saved it again.

- `CLAUDE.md` tells Claude chats not to save sensitive personal details (health, race or ethnicity, religious beliefs, political views, sexual orientation or gender identity) unless the user explicitly asks, matching the Claude apps' default, and that the user's deletes and edits win.

- pi's memory comes in two system prompt sections, the fixed protocol and the index with its notices, and neither depends on the clock any more. pi records a changed section by appending its full text to the conversation, so with the time in it every message re-appended the whole memory text, up to about 26KB. Now the protocol is recorded once per chat and the index only on messages after a note changed. Instead of telling pi the time, `agent_memory_context.py --stamp`, run by the extension right after pi writes or edits a note, sets the note's `metadata.modified` (and `metadata.source` when missing), as Claude Code does for Claude's notes.
