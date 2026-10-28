Chats know who said what (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 5):

- A message from anyone but the workspace's owner (a person the workspace is shared with, or a visiting agent) reaches the agent as `[from: <name>] <text>`, so the agent knows who is asking. Slash commands and bash-mode input are delivered as typed. The owner's messages are unchanged.

- Each chat keeps an `authors.jsonl` beside its transcript, and every `user_message` event the app serves carries `author` (the sender's participant key) and `author_name` when the sender was not the owner. The prefix never shows in the transcript, nor in a send held while the chat switches harness.

- The page draws your own messages on the right as before, and another person's on the left under their name and picture. The agent's replies are drawn as bubbles, one per reply, under the workspace's picture and name.

- New routes: `GET /api/me` (the page's own participant key), `GET /api/participants` (the shell's roster, with the workspace agent's picture served from this app at `GET /api/participants/workspace-agent/picture`).

- The chat list's rows show a kebab beside the rename pencil on hover, opening the row's context menu.

Chat rooms and mentions (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 7):

- A room is a chat among the people in the workspace with no AI agent. A chat waiting for its first send becomes one from its provider chooser ("Chat room (no provider)"), which its provider chip opens (and its first send does, while nothing is signed in), and any chat (the main chat included) becomes a room, or a room becomes an agent chat, from the composer's provider menu: the switch keeps the conversation so far as the chat's earlier segments, and a chat leaving a room hands its new agent the room's transcript. Rooms sit in the chat list with the other chats, wearing a people glyph, and take every chat verb (rename, delete, stop, start). A message sent into a stopped room is appended to its transcript without reviving its reader, so the room keeps reading as stopped until `mngr message --start <room>` (the send `AGENTS.md` has the agent make; a bare `mngr message` refuses a stopped agent), the row's Restart, or boot starts the reader again.

- A room is an mngr agent of the template's new `room` type: its process reads what `mngr message <room>` types into its pane into the room's transcript, which `mngr transcript <room>` renders and the chat app tails. The chat app appends people's messages to the same transcript, so every message in a room is drawn as its author's turn under their name and picture, the owner's unmarked.

- Typing `@` in a room's composer opens a list of handles: the people here, `@agent` for the workspace's agent, and every chat by its title (`@Main-chat`). Mentioning `@agent` or a chat from a room (a room's first message included, once the room is up) sends that chat the message, marked as said in the room, and the room's row shows a working dot until the agent answers into the room (a chat that refuses the message, as one whose account was signed out does, is not waited on). Mentioning a person marks the room's row with an `@` for them until they look at it, and tells the workspace's owner through the Imbue Studio app's feed as the room itself, so the notification opens the room. In an agent chat a handle is a word the agent reads, and the composer offers no popover.

- The chat snapshot carries `kind` (`agent` or `room`) and `awaiting_chat_ids`; `POST /api/chats/create` and `POST /api/chats/<id>/handoff` take `kind` (a room is refused beside an account or a model, before anything is written); `GET /api/mentions` and `GET /api/mentions/pending` are new, with the `mentions_updated` push on `/api/ws`. There are no room-only routes, no room store, and no second chat id shape.

The chat app feeds the desktop's tray and makes the Lounge (the mngr repo's `specs/multiplayer-refinements/spec.md`, sections 6 and 7.5):

- Every message a person or a visiting agent sends (a new chat's first message included), and every turn the workspace agent closes with text in a chat some page is reading (its post into a room included; the turns reach the feed through the session watcher a page's stream builds, so a chat nobody has open raises none), is posted to the shell as a speech bubble (`POST /api/bubbles`, with the people a room message mentions), and every chat-list change is posted as what each chat is doing (`POST /api/chats-activity`), both best-effort and off the request thread; the last chats-activity report is posted again once a minute, so a shell restarted since paints the agent's card. A secondary chat posts neither.

- The Lounge is the chat titled "Lounge", of either kind, found by title alone (a Lounge converted into an agent chat, or a chat so named, is the Lounge). A room under that title is made the first time the roster holds anyone besides the owner (a person, an invited email, or a visiting agent), from the roster the shell answers (read once a minute), and again when a click on a person needs the Lounge after it was deleted or renamed, only while no chat of any kind carries the title. The new `lounge` intake target resolves to it, making the room when there is no such chat, and the manifest's `lounge` launch path (`mention_param = "message"`) drafts `@<handle> ` into it when a person's tray entry is clicked.

- The README's intake paragraph names the `lounge` target and every launch path the manifest declares, and the manifest's comments on the `main`, `new`, and `send` paths name the key each answers (`main` is the launcher's Enter, `new` its Ctrl+Enter, `send` is bound to none, as `draft` is).

The chat app's browser test of a desktop switch drives the shell's overview (the taskbar's "Manage Desktops" button and its thumbnails) rather than the desktop switcher the shell no longer draws (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 8.2); nothing in the chat app itself changes.

The chat about an app (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 9.2):

- The manifest declares `app-chat` ("Chat about this app", `POST /api/chats/intake`, params `app` and `app_display_name`, preset `target = "app_chat"`, `app_params = {name = "app", display_name = "app_display_name"}`), which a window's title bar balloon runs; never a launcher row.

- The new `app_chat` intake target answers the chat labelled `app_chat` with the `app`'s registered name (so a renamed app keeps its chat), else the one titled "<app_display_name> Chat" by the rule a create refuses a taken name by (canonical forms compared case-insensitively, so a chat titled "docs chat" by hand is found rather than the create refused beside it), of either kind or still being made, else creates one with that title and label on the default account (never auto-named), else, with nothing signed in, mints one awaiting its first send under them (`mint_awaiting_chat` takes a name and labels). The intake is always held, even with nothing to send, with a new `hint` ("How do you want to change <app>?") on the pending intake's view and its apply answer; the chat root puts the hint on the chat's composer as its placeholder until the next send and focuses it (`setComposerHint`, the page's embed API's `hintComposer`). An applied intake with nothing to draft or launch now answers `null` for `composer_text` and `first_message` rather than an empty string. The chat app's own windows open "Chat Chat".

- The chat's pinned entry defaults to the bar (the manifest drops `default_mode = "floating"`): the avatar sits in the tray among the people, and a client floats it from the entry's menu.
