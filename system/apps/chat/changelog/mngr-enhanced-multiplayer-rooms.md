Chat rooms and mentions (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 7):

- A room is a chat among the people in the workspace with no agent. "New room" beside "New chat" makes one; a chat waiting for its first send can become one instead of starting an agent ("Chat room (no provider)" in the provider chooser). Rooms sit in the chat list with the other chats, wearing a people glyph, and offer Rename and Delete.

- A room's page is the chat page: every message is drawn as the person's turn, under their name and picture. The composer says "Message (tag @agent to ask the agent)" and the model bar reads "Chat room".

- Typing `@` in any composer opens a list of handles: the people here, `@agent` for the workspace's agent, and every chat by its title (`@Main-chat`). Enter or a click inserts one.

- Mentioning `@agent` or a chat from a room sends that chat the message, marked as said in the room; the agent answers into the room with `python3 system/scripts/chat_room.py post <room> -m "..."`, and the room's row shows a working dot until it does. In an agent chat, `@agent` is just a word the agent reads.

- Mentioning a person marks the chat's row with an `@` for them until they look at it, and tells the workspace's owner through the Imbue Studio app's feed.

- New routes: `GET`/`POST /api/rooms`, `POST /api/rooms/lounge`, `POST /api/rooms/<id>/rename`, `DELETE /api/rooms/<id>`, `GET`/`POST /api/rooms/<id>/messages`, `POST /api/rooms/<id>/post` (loopback), `GET /api/mentions`, `GET /api/mentions/pending`, `POST /api/chats/awaiting/<id>/make-room`; the `rooms_updated` and `mentions_updated` pushes on `/api/ws`; `kind` on every chat snapshot.
