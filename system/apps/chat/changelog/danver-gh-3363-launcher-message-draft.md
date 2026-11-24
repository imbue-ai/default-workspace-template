A message sent to a chat from outside its page while the chat's first agent is still starting is sent, where it used to be left in the composer unsent.

- A text that reaches a chat whose first agent is being created (the launcher's Enter, a Getting Started tile, an agent's `workspace-layout open chat --launch ...`) is kept for that agent and delivered as it comes up, as a message typed in the chat itself already was. Before, it was put into the chat's composer as a draft, and nothing said it had not been sent. A message sent from the launcher right after leaving a new workspace's provider page ran into this every time, since leaving that page starts the main chat's agent.

- The page of a chat that opened on a conversation (a new workspace's main chat) shows a message kept for its agent, under that conversation, while the agent starts. Before, only the page the message was typed in showed it: another page of that chat, or the same page after a reload, showed nothing until the agent took the message.

- A message kept this way for a chat that opened on a conversation and was started with no message (a new workspace's welcome) reaches the agent with that conversation ahead of it, as the first message sent once the agent is up does. Before, the agent got the words alone, and the message after it carried the conversation instead. The same goes for a message typed in the chat's own page while its agent starts.

- `POST /api/chats/intake` answers `/?chat=<id>` for such a text, and `POST /api/chats/intakes/<token>/apply` answers neither `composer_text` nor `first_message` when the chat's create began after the intake was held.
