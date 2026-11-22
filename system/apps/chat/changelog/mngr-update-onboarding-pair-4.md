- New route `GET /api/suggestions/activity?placement=activity|new_chat&chat_id=<chat-id>`: the things worth trying in Studio that the workspace's owner has not done yet (start a second chat, open Studio on a phone, build an app, connect some data), each an action suggestion. It answers nothing to a visitor, with the placement switched off, before the tour has finished, or in the first-run chat while its first conversation follows its tree.

- New route `POST /api/suggestions/activity/clicked`, which records a click; a click on "Connect some data" retires that suggestion.

- A suggestion gains `action`, null for a reply suggestion, which every next-step suggestion is.

- `POST /api/chats/create` takes an optional `client_id`, naming the page a person asked for the chat from.

- The chat app sets three onboarding markers: `second_chat_started` when a person starts a chat (through the create route, or through an intake that makes a new chat, as the shell launcher's "New Chat" does) while another chat a person talks to exists, `permission_request_filed` when it reads a filed permission request, and `connect_data_coach_shown` on the click above.

- While the agent works on a reply, a "While you wait" strip above the composer offers the workspace's owner up to three things worth trying in Studio; it closes into a toggle when the reply ends. A chat nobody has said anything in shows the same offers as "Things to try". Only one strip has its pills shown at a time, and a collapsed one of these two is a toggle, the two side by side in one row.

- Clicking one of these sends nothing: it points a bubble at the chat list's New chat button, at the message box, at the Imbue Studio app's permissions button, or at the phone-access control of its share panel. A suggestion whose bubble this page cannot have drawn is not offered.

- Each of the two strips has its own "disable ..." link with Undo under its pills. The next-step one, "disable next step suggestions", is at the left end of the next-step line while its pills are shown.

- A strip's suggestions are a list on the user's side of the composer's column: one pill to a row, all as wide as the widest, set in 16px from its right edge, with a light shadow.

- A reply's suggestions wait on one quiet line directly above the composer, whose height is always kept in a chat with an agent so their arrival moves nothing: "See N suggestions" shows them above it and becomes "Hide suggestions". The first conversation's fixed sets start shown and a generated set hidden. Every suggestion is drawn, and the "Next steps" toggle and "Show more" are gone.

- An answered reply keeps its suggestions: on a send or a turn's start they move to the transcript as the same line under that reply, hidden until asked for, and a pill clicked there is sent. `GET /api/chats/<chat-id>/suggestions` gains `sets`, every set the chat app remembers for the chat (in memory, the newest 200), so after it restarts only the last reply's is left.

- The chat list's New chat button names the browser it was pressed in (`client_id`) when it creates a chat, as does "Start a new chat" in the switch dialog and the failed-switch notice, and a chat waiting for its first send names it when it launches.

- The first run's provider page reads "Studio runs your agents and apps 24/7. Connect your provider to start.", and "Add another (optional)" over "Studio can use multiple subscriptions and switch between providers mid-conversation." The first account connected there is pinned as the workspace's default, so connecting a second provider no longer moves the first conversation onto it. With two or more connected, the page marks the default and a click on another connected account makes that one the default.

- A first-run chat's start suggestions stay shown while the agent the provider page started comes up: a chat whose first agent is starting with nothing sent to it counts as having no turn under way, for its suggestions and for a picker in a message.

- The category picker offers Books, Movies and Music in place of "Books, movies & music", and each picker ends in an "Other" pill that puts a prompt to write over in the draft ("Write whatever you want!", "Write whatever data you want here!").

- The prompt that works out next-step suggestions asks for a question's own answers when the reply's one main question has a small set of them.

- A chat page says when a reply starts or stops being under way in its chat (`shell:agent-working {isWorking}`), and the chat root tells the shell an agent is working while any chat page it holds says so, so the show-around tour starts on every harness.
