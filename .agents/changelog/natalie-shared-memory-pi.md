pi chats now share the workspace's memory with Claude chats. A new reference, `memory-protocol.md`, is the note format and the rules for keeping notes, adapted from Claude Code's own memory prompt so both harnesses write notes the other reads.

The memory protocol tells pi not to save sensitive personal details (health, race or ethnicity, religious beliefs, political views, sexual orientation or gender identity) unless the user explicitly asks, matching the Claude apps' default.

The memory protocol no longer asks pi to write `modified` itself: the pi extension stamps it when a note is saved, so the date is never guessed.
