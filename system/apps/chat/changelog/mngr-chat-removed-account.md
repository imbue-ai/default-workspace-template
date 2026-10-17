- Removing a provider account no longer breaks new chats. The New chat button used to name the account from the chat list's own copy of the account list, so after an account was removed every click was refused while the list still showed a new chat as if it had started. It now leaves the choice to the chat app, which picks a signed-in account as the chat is created; with nothing signed in it opens the chat that asks you to choose a provider, before anything is created.

- When the chat app refuses to start a new chat, the chat list now says why in a notice, in plain words ("Connect an AI provider to start chatting.", or that the account it was going to use was signed out), instead of the account store's "no such account: <id>".

- "Try again" on a chat that could not be started now starts it on a signed-in account when the one it was made for has been removed, and opens the provider chooser when nothing is signed in, rather than repeating the same refusal.

- A message refused because the chat's account was signed out now offers "Choose a provider" instead of Retry and Force, which only sent it into the same refusal again. The message stays in the message box and goes out on the chosen provider.

- A model change the chat refuses (for example while it is switching providers) now shows its reason in the chat, and the model chip goes straight back to the model the chat is running, instead of a console line reading "Error: [object Object]" and a chip stuck on the refused pick.
