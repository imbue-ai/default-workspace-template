Chat rooms and mentions (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 7):

- `system/scripts/chat_room.py` reads a chat room (`read <room> [--since <event_id>] [--limit N]`, by id or title) and posts into one as the workspace's agent (`post <room> -m "..."`); naming the Lounge makes it.

- `AGENTS.md` tells the agent what a `[from: <name> in #<room>]` prefix means and how to read and answer the room.
