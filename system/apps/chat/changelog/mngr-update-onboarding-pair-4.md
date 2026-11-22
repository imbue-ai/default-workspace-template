- New route `GET /api/suggestions/activity?placement=activity|new_chat&chat_id=<chat-id>`: the things worth trying in Studio that the workspace's owner has not done yet (start a second chat, open Studio on a phone, build an app, connect some data), each an action suggestion. It answers nothing to a visitor, with the placement switched off, before the tour has finished, or in the first-run chat while its first conversation follows its tree.

- New route `POST /api/suggestions/activity/clicked`, which records a click; a click on "Connect some data" retires that suggestion.

- A suggestion gains `action`, null for a reply suggestion, which every next-step suggestion is.

- `POST /api/chats/create` takes an optional `client_id`, naming the page a person asked for the chat from.

- The chat app sets three onboarding markers: `second_chat_started` when a person starts a chat while another chat a person talks to exists, `permission_request_filed` when it reads a filed permission request, and `connect_data_coach_shown` on the click above.
