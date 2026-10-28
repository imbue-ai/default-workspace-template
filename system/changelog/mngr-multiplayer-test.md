Multiplayer, part one (the mngr repo's `specs/multiplayer-refinements/spec.md`, sections 4 and 5): the workspace knows who is here, and chats know who said what.

- `AGENTS.md` tells the agent, under "Communication", that a message prefixed `[from: <name>]` came from someone other than the workspace's owner, and that the prefix is not part of what they said.

- The desktop interface contract (`docs/system/blueprint/desktop-interface/contracts.md`) gains the participant key (section 4.3), the heartbeat's input time and the roster route (5.1), the client inventory's participants (5.5), and the `roster_updated` push (6).

- The grants parsers the share gateway and the shell both need now live in one library, `system/libs/share_grants`.

Chat rooms (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 7):

- The new `room` agent type (`[agent_types.room]` in `.mngr/settings.toml`): a chat room's agent, whose process, `system/scripts/room_agent.py`, appends what `mngr message <room>` types into its pane to the room's common transcript, so `mngr transcript <room>` reads a room and `mngr message <room>` posts into one as the workspace's agent. It reads its pane unbuffered, so a pasted paragraph longer than a terminal line arrives whole, and launches through the OOM band wrapper like every chat.

- `system/scripts/require_create_account.py` lets a create through on no account when `MINDS_CREATE_WITHOUT_ACCOUNT=1` is set, which the chat app sets for a room's create.

- `AGENTS.md` tells the agent what a `[from: <name> in #<room>]` prefix means and how to read and answer the room with `mngr transcript` and `mngr message`.

`system/scripts/forward_port.py` copies a launch path's `mention_param` onto the registry row beside its `text_param` and `draft_param` (the multiplayer spec, section 7.5).

`workspace_layout`'s `EntryPresentation` gains `is_window_shown` (default true): whether a client lists a pinned window in its taskbar, false after that client closed it.

The desktop interface's contract table (`docs/system/blueprint/desktop-interface/contracts.md`, section 2) lists the chat app's `lounge` launch path beside its other intake paths, and `system/test_app_manifests.py` holds the manifest to it.

The post-launch-paths plan (`docs/system/blueprint/post-launch-paths/plan-post-launch-paths.md`) lists the `lounge` intake target and launch path beside the others, and its launcher section (4) names the rows the chords run now that `main` is first: `main` is Enter, `new` is Ctrl+Enter, and a Getting Started tile's text goes to the main chat; the launcher plan (`docs/system/blueprint/launcher-and-getting-started/plan-launcher-and-getting-started.md`, sections 3.1, 3.5, 4.4, and 4.5) names the same rows in its stock-machine examples.

The desktop-interface blueprint (`docs/system/blueprint/desktop-interface/`) records the desktops phase of the multiplayer spec (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 8): the desktop record's `owner` and the display order the `POST /api/desktops/order` route rewrites in `contracts.md` (sections 4.1 and 5.2, and the overview's, chip's, and backdrop menu's selectors in section 12, beside the tray's, which replace the retired Presence widget's), the overview and viewing another person's desktop in `concepts.md` (sections 2.1 and 2.8), and the plan's view list naming the overview and the wallpaper chooser in place of the desktops widget and the settings dialog.

`system/test_app_manifests.py` also holds the terminal, files, and browser launch paths to declaring no `mention_param`, as it already did for their `text_param` and `draft_param`.

The contract table's chat row (`docs/system/blueprint/desktop-interface/contracts.md`, section 2) lists the `main` launch path ("Send to main chat", `target = "main_chat"`) the manifest declares and the manifest test asserts, which the row had left out since the path was added.

The app-chat phase of the multiplayer spec (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 9): `system/scripts/forward_port.py` copies a launch path's `app_params` pair onto the registry row; the new `system/scripts/rename_chat.py` renames the chat under one title to another through the chat app's rename route, for an app whose display name changed; the desktop-interface blueprint (`contracts.md` sections 2, 5.3, 5.5, and 12; `concepts.md` section 2.7) and the post-launch-paths plan (the `show` launch target, the `app_chat` intake target) record the title bar, the launch target, and the launch path field; `system/test_app_manifests.py` holds the chat manifest to its seventh launch path.
