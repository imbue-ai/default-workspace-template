Multiplayer, part one (the mngr repo's `specs/multiplayer-refinements/spec.md`, sections 4 and 5): the workspace knows who is here, and chats know who said what.

- `AGENTS.md` tells the agent, under "Communication", that a message prefixed `[from: <name>]` came from someone other than the workspace's owner, and that the prefix is not part of what they said.

- The desktop interface contract (`docs/system/blueprint/desktop-interface/contracts.md`) gains the participant key (section 4.3), the heartbeat's input time and the roster route (5.1), the client inventory's participants (5.5), and the `roster_updated` push (6).

- The grants parsers the share gateway and the shell both need now live in one library, `system/libs/share_grants`.
