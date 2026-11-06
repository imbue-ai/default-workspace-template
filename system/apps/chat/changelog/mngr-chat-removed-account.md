- Removing a provider account no longer breaks new chats. The New chat button used to name the account from the chat list's own copy of the account list, so after an account was removed every click was refused while the list still showed a new chat as if it had started. It now leaves the choice to the chat app, which picks a signed-in account as the chat is created; with nothing signed in it opens the chat that asks you to choose a provider, before anything is created.

- When the chat app refuses to start a new chat, the chat list now says why in a notice, in plain words ("Connect an AI provider to start chatting.", or that the account it was going to use was signed out), instead of the account store's "no such account: <id>".

- "Try again" on a chat that could not be started now starts it on a signed-in account when the one it was made for has been removed, and opens the provider chooser when nothing is signed in, rather than repeating the same refusal.

- A chat whose account is removed in another chat or window now shows "You signed out of the account this chat runs on." with "Choose a provider" right away, instead of only after a message to it is refused. The chat app tells every open page when the account list changes, and a page re-reads the list then and whenever its connection comes back.

- A message refused because the chat's account was signed out now offers "Choose a provider" instead of Retry and Force, which only sent it into the same refusal again. The message stays in the message box and goes out on the chosen provider.

- A model change the chat refuses (for example while it is switching providers) now shows its reason in the chat, and the model chip goes straight back to the model the chat is running, instead of a console line reading "Error: [object Object]" and a chip stuck on the refused pick.

- Moving a chat that has nothing said in it to another provider no longer shows "Handing off to …" while the new agent starts: with no conversation there is nothing to hand off, so only "Starting …" shows.

- A chat restarting on another account says "Restarting …" once, by the message box, instead of also at the top of the conversation.

- Moving a chat to another account of the same provider without typing a message (picking the account from the provider menu or the chooser) no longer leaves the chat waiting about a minute and a half before it is ready: it used to send the restarted agent an empty message and wait for a delivery that could never be confirmed.
