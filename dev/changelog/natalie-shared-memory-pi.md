pi chats keep the workspace's shared memory (`data/memories/`) alongside Claude chats.

- New `.pi/extensions/memory.ts` puts the memory protocol and the current `MEMORY.md` index in every pi turn's system prompt, as its own section (or appended, when another extension already replaced the prompt for that turn). It fails open.

- pi's system prompt in `.mngr/settings.toml` no longer tells it to avoid memory; it points it at that section instead.

- `AGENTS.md` and `data/memories/README.md` describe the folder as shared by Claude and pi chats.
