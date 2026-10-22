The shell knows who is here (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 4):

- `GET /api/roster` lists the workspace's participants: the owner first, then everyone the workspace is shared with (by account, by invited email, and visiting agents), and the workspace's own agent last, each with a display name, picture, a mention handle, a colour, and whether they are connected and when they were last seen and last active. A `roster_updated` push on `/api/ws` follows every change, and the roster is sent on connect.

- Every request's identity resolves to a participant key (`owner`, `user:<id>`, `agent:<id>`); the heartbeat answers the page its own key and records when the person last typed or clicked, and the owner of an unshared workspace is now counted as present under the `owner` key.

- A visiting agent's presence comes from the share gateway's last-seen record (which it now refreshes every 10 seconds), so an agent with a grant shows as connected while it is calling in.

- The client inventory (`/api/clients`) names each client's participant.

- The grants parsers moved to the shared `share_grants` library; `shell/identity.py` moved to `workspace_layout.identity`.
