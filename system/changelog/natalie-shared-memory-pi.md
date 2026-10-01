pi chats keep the workspace's shared memory (`data/memories/`) alongside Claude chats.

- New `.pi/extensions/memory.ts` puts the memory protocol and the current `MEMORY.md` index in every pi turn's system prompt, as its own section (or appended, when another extension already replaced the prompt for that turn). It fails open.

- New `system/scripts/agent_memory_context.py` prints what that section holds: the protocol filled in with the notes folder, the harness and the current UTC time (Claude Code stamps a note's `modified` itself; a model left to guess the date gets it wrong), then the index. It is standard-library only, since pi runs it under the system `python3`, and listed in `stdlib_only_scripts_test.py`.

- pi's system prompt in `.mngr/settings.toml` no longer tells it to avoid memory; it points it at that section instead.

- `AGENTS.md` and `data/memories/README.md` describe the folder as shared by Claude and pi chats.
