Chat failures that have a way out beyond trying again now offer it as a button.

- Switch provider: the page of a chat whose create failed has "Switch provider" beside "Try again". It opens the provider chooser with the failing account marked, and launches the chat on the one picked. The notice for a send whose agent couldn't be started has it too, and so does a failed turn that hit a rate or usage limit ("You've hit your session limit") or a provider fault; there it switches the chat to the provider picked.

- Ask an agent to fix this, for a chat that can't start: the failed-create page, and the notice for a send to a chat whose agent wouldn't start or whose create failed, send what failed to an agent that is already running (the main chat, else the agent chat messaged most recently) and show that chat. The button says why if the send is refused, and is not offered when no other agent chat exists.

- A conversation too long for the model: the failed turn has "Start a new chat" and "Compact", which sends `/compact`. A `/compact` that is declined or fails says so in a notice, and both buttons stay.
