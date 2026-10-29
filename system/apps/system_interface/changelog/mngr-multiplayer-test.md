The shell knows who is here (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 4):

- `GET /api/roster` lists the workspace's participants: the owner first, then everyone the workspace is shared with (by account, by invited email, and visiting agents), and the workspace's own agent last, each with a display name, picture, a mention handle, a colour, and whether they are connected and when they were last seen and last active. A `roster_updated` push on `/api/ws` follows every change, and the roster is sent on connect.

- Every request's identity resolves to a participant key (`owner`, `user:<id>`, `agent:<id>`); the heartbeat answers the page its own key and records when the person last typed or clicked, and the owner of an unshared workspace is now counted as present under the `owner` key.

- A visiting agent's presence comes from the share gateway's last-seen record (which it now refreshes every 10 seconds), so an agent with a grant shows as connected while it is calling in.

- The client inventory (`/api/clients`) names each client's participant.

- The grants parsers moved to the shared `share_grants` library; `shell/identity.py` moved to `workspace_layout.identity`.

The taskbar's tray becomes where the people and the agent live (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 6):

- The tray is the share button (a dashed circle with a plus, drawn only under an Imbue Studio chrome, which it asks to open the share panel on the whole workspace), one picture per person in the roster (faded while they are away, a dashed envelope for an invited email, a monogram tile for a visiting agent, a neutral silhouette for the owner of a workspace that has not shared, stacked from four on and spread while the pointer is over them), your own picture, and the agent's avatar -- the one avatar creature's docked home, and its default: a client may float it over the windows instead (the pinned entry's Float row), which draws it there and not in the tray. An entry hops when it arrives and when a bubble of its appears. The desktop switcher moved to the left of the window entries, where the desktops phase puts the overview button.

- The chat's pinned window wears a plain taskbar entry like every other window's, whatever the avatar's mode, and its Close closes it for that client alone: the window is minimized and its entry leaves that client's bar until something shows the window again (a launch, the floating avatar's or the launcher's click).

- Hovering a person opens their card: the name, whether they are active, idle, offline, or invited, what they are looking at (the window at the top of the stack on their newest client), and their desktops, with "New desktop" on your own. Hovering the agent opens its card: the workspace name with a pencil to the avatar chooser, whether it is working, and the chats it is working on and the ones waiting on you, which open on a click. Clicking a person drafts a mention of them into the Lounge through the first launch path of the machine that declares a `mention_param` (a launch path a desktop is never seeded with a shortcut of, as with one taking text); clicking the agent opens the main chat.

- Speech bubbles: the chat app posts every message somebody sends and every turn the agent closes with text to `POST /api/bubbles`, and the shell broadcasts each as `bubble` to every window but the speaker's own clients (the owner's under either of their keys). A page already looking at the bubble's chat does not raise it (a mention of the viewer still rises). A bubble hangs over the speaker's entry for five seconds after its last change; a person's new message replaces theirs, the agent's new message within its bubble's lifetime is appended ("and N more" past three lines), bubbles of different speakers stack, and a mention of the viewer wears an accent border and stays until dismissed or its chat is opened. A click opens the chat it was said in.

- `POST /api/chats-activity` takes what every chat is doing from the chat app, kept and broadcast as `chats_activity` (sent on connect once reported); `roster_updated` is read by the page now, so the tray draws the roster rather than the connected users alone.

Desktops belong to people by convention, and the desktop switcher becomes an overview (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 8):

- A desktop carries an `owner`: the key of the visitor it was seeded for on their first arrival, or created by (`POST /api/desktops` writes the requester's key; the workspace owner's desktops, and every desktop from before, carry none). "Mine" is a keyless desktop for the owner, or one carrying your key. A desktop whose owner has left the roster is left behind: the owner's overview lists it under "Left behind", to view or delete. There is no access control.

- The taskbar's desktop switcher is replaced by the overview button (four squares, "Manage Desktops") between the launcher field and the window entries. The overview tiles the active desktop's windows as live, inert, scaled tiles (a click raises one) over thumbnails of the viewer's own desktops drawn from their wallpapers and this client's placements: a click switches, a double-click on the name renames, a drag sideways reorders (the new `POST /api/desktops/order`, which lists the requester's own desktops in a new order and leaves the others in place), a right-click offers "Delete desktop..." confirmed in place (the menu ends with the element reference rows, as the backdrop's does), and the plus makes "Desktop N". Escape or a click on the bare backdrop closes it. The backdrop's context menu offers "New desktop", "Show all desktops", and "Change wallpaper...", which opens the wallpaper chooser (the settings dialog pared down to it); a desktop's colour and glyph are no longer shown or edited, though the record keeps them. The launcher's window rows name the owner of a desktop that is not the viewer's.

- A person's tray card lists the desktops they own, and a click on one of somebody else's views it: the client switches there with the viewing chrome on (an outline in the owner's colour around the whole desktop, and a chip in that colour with their picture, "<Name> · <Desktop name>", and "Stop viewing" in place of the tray, the overview button hidden and the backdrop menu's "Show all desktops" disabled); Stop viewing goes back to the desktop the window came from, else the viewer's first. Every window verb works on a viewed desktop. Nothing is viewed until the page's first heartbeat has said who the viewer is, so a visitor's own desktop never wears the chrome while it is on its way. On other clients the owner of a desktop somebody else is on wears a ring in their colour in the tray.

- `POST /api/desktops` and `POST /api/desktops/<id>/settings` take `color` and `glyph` as optional (absent: the shell picks the next free glyph and its colour for a create, and a settings post keeps them as written); the frontend sends the name alone. The desktop object and `desktops.json` carry `owner`.

Every window gets "Chat about this app" (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 9):

- A focused window's title bar is now the window menu, Refresh, and a speech balloon, "Chat about this app", on the left, the app's icon and title centred over the bar, and minimize, maximize, and close on the right as before (`data-window-control="app-chat"`; the menu and Refresh swap places). The balloon is drawn on every window of an app that is not internal, the chat app's included, when some app declares a launch path with `app_params`.

- The balloon runs the first such launch path (the chat app's `app-chat`), naming the window's app and its display name in the params it declares, through the launch route's new `show` target: the page lands where the `show` op would put it (a window already there raised, else the app's pinned window pointed at it for this client, else a new window) and, since the target names the window, the two are snapped side by side for this client, the chat on the half of the backdrop its frame's centre was already on (the right when it is minimized or was never placed) and the app's window on the other, the chat on top; nothing is laid out when the window shown is the one the balloon is on (the pinned window's own, which the show navigates) or when either window is pulled out into a window of the chrome's own, which the show leaves where it is. The launch target `{"kind": "show", "beside"?}` is open to any caller; the shell names no app in any of this.

- `POST /api/desktops/<id>/launch` takes the `show` kind; the `app` object's launch paths carry `app_params`.

- Viewing somebody else's desktop shows it as they have it, live and read-only: the placements read gains `?of=owner` (the desktop as its owner last arranged it, from their most recently seen client with a written layout), which a viewing window reads in place of its own layout, refreshed on every `placements_updated` push for the desktop, by a slow backstop poll, and the moment the first heartbeat says who the viewer is; the pages are inert the way the overview's tiles are, the windows and the taskbar's entries take no press, and nothing a viewing window holds is saved as the viewer's layout.

- The overview's rename works and is discoverable: the rename input keeps what you type through the shell's constant redraws (it was a controlled value that every redraw reset, so typing appeared dead), the thumbnail's right-click menu offers "Rename" beside "Delete desktop...", and the delete confirmation hangs above the thumbnail rather than clipping past the bottom of the screen.

- The agent's hover card trades its pencil for action rows -- "Change avatar..." and "Float" -- and the tray's agent entry takes a right-click menu with the same two, the way the floating creature offers its own.

- Presence reads real activity: the active/idle call used to see only the shell window's own pointer and key events, so everyone read as idle since they last touched the chrome; the app pages' `shell:input` reports now feed the heartbeat's `last_input_at`.

- The owner-layout read (`?of=owner`) answers without a `client` parameter, which is how a viewing window asks for it; it used to refuse that request, so a viewed desktop showed its default (empty) layout rather than its owner's.

- A person's card marks the desktop they are on ("There now", with a dot in their colour) beside the "Viewing" mark of the one you are on, read off their newest connected client as the card opens.

- The overview's live tile pages step aside while a desktop's deletion is being confirmed, since the confirmation hangs up into the grid and the pages, laid above the overview's scrim, covered it.
