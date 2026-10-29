- `workspace_layout.identity` (moved from the shell) parses the `X-Imbue-Identity` header into a `RequestIdentity` and resolves it to a `ParticipantKey` (`owner`, `user:<id>`, `agent:<id>`); `workspace_layout.roster` carries the roster document (`RosterParticipant`, `RosterDocument`), the mention handles, and the participant colour palette.

- The shell client gains `roster()` for `GET /api/roster`, and the client inventory's records name each client's participant.

`ShellLayoutInterface` gains two best-effort posts an app makes to the shell for the taskbar's tray (the mngr repo's `specs/multiplayer-refinements/spec.md`, sections 6.4 and 6.5): `post_bubble` (`POST /api/bubbles`, a `BubblePost`: who said what, in which chat, mentioning whom) and `report_chats_activity` (`POST /api/chats-activity`, a `ChatsActivityReport`: every chat's title, status, and kind). The models live in `workspace_layout.feed`, with `ChatActivityStatus` and `ChatActivityKind` in the primitives; `ShellLayoutClient` posts them as it posts a client-activity report, `DisconnectedShell` drops them, and the testing `FakeShell` records them.

`Desktop` (and so the `desktop` object every listing, op answer, and `desktops_updated` carries) gains `owner`: the participant key of the person the desktop belongs to by convention, or `None` for the workspace owner's, which every desktop from before the key was kept is (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 8.1). `DesktopsListing` describes its desktops as being in display order: creation order, as the shell's `POST /api/desktops/order` rearranged it.

- `RequestIdentity` gains `is_header_present`, set only by the header parse: it tells a page's request (a proxy stamped it) from an in-container caller's (a script's, the terminal's), which both read as the owner otherwise -- what lets the chat app annotate the owner's page sends without counting scripts as the owner writing.

- `ClientRecord` (and so the client objects of the clients listing and the inventory) gains `viewport`: a `Viewport` (width and height in CSS pixels) of the backdrop the client's desktop layout last reported its frames as fractions of, or `None` for a client that never has (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 8.3); a window viewing one of the client's desktops draws the layout at that size.

