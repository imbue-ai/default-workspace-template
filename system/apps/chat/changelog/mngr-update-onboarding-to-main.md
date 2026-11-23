The chat now offers suggestions: short replies above the composer that the user can click instead of typing.

- When a chat has suggested replies to its last message, they appear as a list of pills above the composer. Clicking one sends it as a message; with text already in the composer, that text is sent with the suggestion on a new line, along with any attachments. Once a message is sent, they stay with the reply they were made for. A chat room shows none.

- A new workspace's first chat opens on a shorter welcome that ends in "How do you want to start?", over two suggestions: "Describe what you want to make" and "See what Studio can do". Once a provider is connected the agent no longer speaks first: it waits for a pick or a typed message, and answers a pick with a prepared reply (the second also opens the Getting Started app beside the chat). What an older Imbue Studio app says the user wants Studio for still reaches the agent.

- The workspace's owner can turn the suggestions off with the "disable suggestions" link beside them, with "Undo" offered for a few seconds. The chat settings gain two switches, both on by default: `is_next_step_suggestions_enabled` and `is_new_chat_suggestions_enabled`. `PUT /api/settings/suggestions` changes either or both and leaves every other setting as it is, and every settings change is pushed to the open pages as `settings_updated` on `/api/ws`.

- `GET /api/chats/<chat-id>/suggestions` answers the suggestions stored for a chat (`suggestions.json` in the chat's folder), or `none`.

The chat now works out its own suggested replies.

- When an agent finishes a reply, the chat suggests two or three replies to it a few seconds later, as the pills above the composer. One may be marked as the recommended one, in green. Nothing is suggested when no reply would make sense, under a reply that is a provider failure, in a chat room, or in a chat an agent started.

- The suggestions come from one call per finished reply to a mid-sized model (Sonnet for Claude, the account's newest "terra" model for Codex) on the account the chat already runs on, sent the last few turns of the conversation. The call stays outside the chat's transcript. Every chat gets them, whether or not it is on screen; switching "Next step suggestions" off stops the calls. Chats on Pi, Antigravity and OpenCode get none.

- A chat's suggestions never outlive the reply they were made for: the stored set is replaced or removed each time a turn ends.

- `GET /api/chats/<chat-id>/suggestions` answers `pending` while a set is being worked out, and `/api/ws` pushes `suggestions_updated` with the `chat_id` when a chat's stored set is replaced or removed.

A new workspace's first conversation can now help its user find something to build.

- The welcome offers a third start suggestion, "Get ideas for what to make", between "Describe what you want to make" and "See what Studio can do". Picking it has the agent ask what the workspace is for and which apps the user has, and then list ideas that fit both answers.

- The two questions come with pickers drawn in the agent's message: three columns of categories (Work, Life, Creative), and a row of fifteen apps, each with its logo where the workspace has one for it. Clicking a pill adds its name to the message box, and clicking it again takes it out; the user sends the message themselves and can type anything of their own. A picker in a message that has been answered is greyed out. The first category picked in a workspace points a bubble at the send button, "Click this button to send your message", which goes once the message is sent.

- The replies offered under these messages are the chat's own, with no model asked, so they appear the moment the reply ends and work with every AI provider: "Show me some ideas" under the question about apps, and one per idea, then "More ideas" and "Something else", under a list of ideas. Typing anything else at the start or under the ideas ends the scripted conversation, after which the chat gets suggested replies like any other.

- An assistant message that carries `<studio_category_picker/>` or `<studio_connector_picker/>` on a line of its own draws that picker. The chat's folder holds the conversation's step in `first_run_tree.json`, and the chat settings gain `is_picker_send_hint_shown`.

- New route `GET /api/suggestions/new-chat?chat_id=<chat-id>`: the things worth trying in Studio that the workspace's owner has not done yet (start a second chat, open Studio on a phone, build an app, connect some data), each an action suggestion. They are the four onboarding items the `onboarding_activity` library names, which the onboarding checklist shows too. It answers nothing to a visitor, with new chat suggestions switched off, before the tour has finished, or in the first-run chat while its first conversation follows its tree.

- A suggestion gains `action`, null for a reply suggestion, which every next-step suggestion is.

- `POST /api/chats/create` takes an optional `client_id`, naming the page a person asked for the chat from.

- The chat app sets two onboarding markers: `second_chat_started` when a person starts a chat (through the create route, or through an intake that makes a new chat, as the shell launcher's "New Chat" does) while another chat a person talks to exists, and `permission_request_filed` when it reads a filed permission request.

- The chat settings hold two suggestion switches, `is_next_step_suggestions_enabled` and `is_new_chat_suggestions_enabled`, and `PUT /api/settings/suggestions` takes those two. The settings file and both settings routes drop a field they do not know, so a file or a writer that carries one keeps every other value.

- A chat nobody has said anything in shows a strip above the composer offering the workspace's owner up to three of those things. It goes for good on the chat's first message.

- Clicking one of these sends nothing: it points a bubble at the chat list's New chat button, at the message box, at the Imbue Studio app's permissions button, or at the phone-access control of its share panel, every time it is clicked. A suggestion whose bubble this page cannot have drawn is not offered.

- The chat app takes a new message, `onboarding:show-new-chat-pointer` (`POST /api/show-new-chat-pointer`), which another app's page sends through the shell: the chat list comes forward and its New chat button gets the same bubble, with the chat on screen left as it is. The onboarding checklist's first entry sends it.

- Each strip is its pills over one quiet line, which holds the strip's link with Undo: "disable new chat suggestions" at the right end of the new-chat strip's line, and "disable suggestions" immediately left of "Hide suggestions" on the next-step line while its pills are shown.

- A strip's suggestions are a list on the user's side of the transcript's column: one pill to a row, all as wide as the widest, set in 16px from its right edge, with a solid fill and a light shadow. While shown they float over the end of the transcript, directly above the footer, which keeps its height: the transcript scrolls underneath, a click beside the pills reaches it, a reader at its end stays there with the last reply clear of the pills, and one who has scrolled up sees nothing move. A list taller than 60% of the transcript scrolls in place.

- A chat ends closer to the composer: about 20px separates the end of the last reply (its "Done" mark) from the line above the composer, and about 24px from the first shown pill, where about 70px did.

- A reply's suggestions wait on one quiet line directly above the composer, whose height is always kept in a chat with an agent so their arrival moves nothing: "See N suggestions" shows them above it and becomes "Hide suggestions". The first conversation's fixed sets start shown and a generated set hidden, and every suggestion of a set is drawn.

- An answered reply keeps its suggestions: on a send or a turn's start they move to the transcript as the same line under that reply, hidden until asked for, and a pill clicked there is sent. `GET /api/chats/<chat-id>/suggestions` gains `sets`, every set the chat app remembers for the chat (in memory, the newest 200), so after it restarts only the last reply's is left.

- The chat list's New chat button names the browser it was pressed in (`client_id`) when it creates a chat, as does "Start a new chat" in the switch dialog and the failed-switch notice, and a chat waiting for its first send names it when it launches.

- The first run's provider page reads "Studio runs your agents and apps 24/7. Connect your provider to start.", and "Add another (optional)" over "Studio can use multiple subscriptions and switch between providers mid-conversation." The first account connected there is pinned as the workspace's default, so connecting a second provider no longer moves the first conversation onto it. With two or more connected, the page marks the default and a click on another connected account makes that one the default.

- A first-run chat's start suggestions stay shown while the agent the provider page started comes up: a chat whose first agent is starting with nothing sent to it counts as having no turn under way, for its suggestions and for a picker in a message.

- The category picker offers Books, Movies and Music in place of "Books, movies & music", and each picker ends in an "Other" pill that puts a prompt to write over in the draft ("Write whatever you want!", "Write whatever data you want here!").

- The prompt that works out next-step suggestions asks for a question's own answers when the reply's one main question has a small set of them.

- A chat page says when a reply starts or stops being under way in its chat (`shell:agent-working {isWorking}`), and the chat root tells the shell an agent is working while any chat page it holds says so, so the show-around tour starts on every harness.

- The show-around tour waits for a new workspace's first conversation to leave its tree. Every `chats_updated` push carries `is_first_conversation_pending`, true while a first-run chat the app lists stands at a step other than `done`, and the message that ends the tree pushes the chat list at once. The chat root passes it to the shell as `shell:first-conversation-step {isPending}`, so the tour begins with the reply to that message, or with the next one.
