Chats know who said what (the mngr repo's `specs/multiplayer-refinements/spec.md`, section 5):

- A message from anyone but the workspace's owner (a person the workspace is shared with, or a visiting agent) reaches the agent as `[from: <name>] <text>`, so the agent knows who is asking. Slash commands and bash-mode input are delivered as typed. The owner's messages are unchanged.

- Each chat keeps an `authors.jsonl` beside its transcript, and every `user_message` event the app serves carries `author` (the sender's participant key) and `author_name` when the sender was not the owner. The prefix never shows in the transcript.

- The page draws your own messages on the right as before, and another person's on the left under their name and picture. The agent's replies are drawn as bubbles, one per paragraph, under the workspace's picture and name.

- New routes: `GET /api/me` (the page's own participant key), `GET /api/participants` (the shell's roster, with the workspace agent's picture served from this app at `GET /api/participants/workspace-agent/picture`).

- The chat list's rows show a kebab beside the rename pencil on hover, opening the row's context menu.
