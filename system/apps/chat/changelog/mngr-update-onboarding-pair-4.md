- New route `GET /api/suggestions/activity?placement=activity|new_chat&chat_id=<chat-id>`: the things worth trying in Studio that the workspace's owner has not done yet (start a second chat, open Studio on a phone, build an app, connect some data), each an action suggestion. It answers nothing to a visitor, with the placement switched off, before the tour has finished, or in the first-run chat while its first conversation follows its tree.

- New route `POST /api/suggestions/activity/clicked`, which records a click; a click on "Connect some data" retires that suggestion.

- A suggestion gains `action`, null for a reply suggestion, which every next-step suggestion is.

- `POST /api/chats/create` takes an optional `client_id`, naming the page a person asked for the chat from.

- The chat app sets three onboarding markers: `second_chat_started` when a person starts a chat (through the create route, or through an intake that makes a new chat, as the shell launcher's "New Chat" does) while another chat a person talks to exists, `permission_request_filed` when it reads a filed permission request, and `connect_data_coach_shown` on the click above.

- While the agent works on a reply, a "While you wait" strip above the composer offers the workspace's owner up to three things worth trying in Studio; it closes into a toggle when the reply ends. A chat nobody has said anything in shows the same offers as "Things to try". Only one strip is open at a time, and the others are toggles in one row.

- Clicking one of these sends nothing: it points a bubble at the chat list's New chat button, at the message box, at the Imbue Studio app's permissions button, or at the phone-access control of its share panel. A suggestion whose bubble this page cannot have drawn is not offered.

- Each of the two strips has its own "disable ..." link with Undo, as the next-step strip has.

- The chat list's New chat button names the browser it was pressed in (`client_id`) when it creates a chat, as does "Start a new chat" in the switch dialog and the failed-switch notice, and a chat waiting for its first send names it when it launches.
