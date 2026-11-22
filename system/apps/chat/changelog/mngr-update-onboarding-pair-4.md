- New route `GET /api/suggestions/new-chat?chat_id=<chat-id>`: the things worth trying in Studio that the workspace's owner has not done yet (start a second chat, open Studio on a phone, build an app, connect some data), each an action suggestion. They are the four onboarding items the `onboarding_activity` library names, which the onboarding checklist shows too. It answers nothing to a visitor, with new chat suggestions switched off, before the tour has finished, or in the first-run chat while its first conversation follows its tree.

- A suggestion gains `action`, null for a reply suggestion, which every next-step suggestion is.

- `POST /api/chats/create` takes an optional `client_id`, naming the page a person asked for the chat from.

- The chat app sets two onboarding markers: `second_chat_started` when a person starts a chat (through the create route, or through an intake that makes a new chat, as the shell launcher's "New Chat" does) while another chat a person talks to exists, and `permission_request_filed` when it reads a filed permission request.

- The chat settings hold two suggestion switches, `is_next_step_suggestions_enabled` and `is_new_chat_suggestions_enabled`, and `PUT /api/settings/suggestions` takes those two. The settings file and both settings routes drop a field they do not know, so a file or a writer that carries one keeps every other value.

- A chat nobody has said anything in shows a strip above the composer offering the workspace's owner up to three of those things. It goes for good on the chat's first message.

- Clicking one of these sends nothing: it points a bubble at the chat list's New chat button, at the message box, at the Imbue Studio app's permissions button, or at the phone-access control of its share panel, every time it is clicked. A suggestion whose bubble this page cannot have drawn is not offered.

- Each strip is its pills over one quiet line, which holds the strip's link with Undo: "disable new chat suggestions" at the right end of the new-chat strip's line, and "disable suggestions" immediately left of "Hide suggestions" on the next-step line while its pills are shown.

- A strip's suggestions are a list on the user's side of the transcript's column: one pill to a row, all as wide as the widest, set in 16px from its right edge, with a solid fill and a light shadow. While shown they float over the end of the transcript, directly above the footer, which keeps its height: the transcript scrolls underneath, a click beside the pills reaches it, a reader at its end stays there with the last reply clear of the pills, and one who has scrolled up sees nothing move. A list taller than 60% of the transcript scrolls in place.

- A chat ends closer to the composer: about 20px separates the end of the last reply (its "Done" mark) from the line above the composer, and about 24px from the first shown pill, where about 70px did.

- A reply's suggestions wait on one quiet line directly above the composer, whose height is always kept in a chat with an agent so their arrival moves nothing: "See N suggestions" shows them above it and becomes "Hide suggestions". The first conversation's fixed sets start shown and a generated set hidden. Every suggestion is drawn, and the "Next steps" toggle and "Show more" are gone.

- An answered reply keeps its suggestions: on a send or a turn's start they move to the transcript as the same line under that reply, hidden until asked for, and a pill clicked there is sent. `GET /api/chats/<chat-id>/suggestions` gains `sets`, every set the chat app remembers for the chat (in memory, the newest 200), so after it restarts only the last reply's is left.

- The chat list's New chat button names the browser it was pressed in (`client_id`) when it creates a chat, as does "Start a new chat" in the switch dialog and the failed-switch notice, and a chat waiting for its first send names it when it launches.

- The first run's provider page reads "Studio runs your agents and apps 24/7. Connect your provider to start.", and "Add another (optional)" over "Studio can use multiple subscriptions and switch between providers mid-conversation." The first account connected there is pinned as the workspace's default, so connecting a second provider no longer moves the first conversation onto it. With two or more connected, the page marks the default and a click on another connected account makes that one the default.

- A first-run chat's start suggestions stay shown while the agent the provider page started comes up: a chat whose first agent is starting with nothing sent to it counts as having no turn under way, for its suggestions and for a picker in a message.

- The category picker offers Books, Movies and Music in place of "Books, movies & music", and each picker ends in an "Other" pill that puts a prompt to write over in the draft ("Write whatever you want!", "Write whatever data you want here!").

- The prompt that works out next-step suggestions asks for a question's own answers when the reply's one main question has a small set of them.

- A chat page says when a reply starts or stops being under way in its chat (`shell:agent-working {isWorking}`), and the chat root tells the shell an agent is working while any chat page it holds says so, so the show-around tour starts on every harness.
