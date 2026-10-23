Chat rooms (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 7):

- The new `room` agent type (`[agent_types.room]` in `.mngr/settings.toml`): a chat room's agent, whose process, `system/scripts/room_agent.py`, appends what `mngr message <room>` types into its pane to the room's common transcript, so `mngr transcript <room>` reads a room and `mngr message <room>` posts into one as the workspace's agent. It launches through the OOM band wrapper like every chat.

- `system/scripts/require_create_account.py` lets a create through on no account when `MINDS_CREATE_WITHOUT_ACCOUNT=1` is set, which the chat app sets for a room's create.

- `AGENTS.md` tells the agent what a `[from: <name> in #<room>]` prefix means and how to read and answer the room with `mngr transcript` and `mngr message`.
