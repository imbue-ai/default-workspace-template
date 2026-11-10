On a new workspace with no AI account signed in, the provider chooser now opens as soon as the chat list is on screen. It no longer waits for the welcome chat to be seeded or for the chat app's first read of the agent list.

- `POST /api/focus-chat` takes a missing `chatId` to mean "show the chat list as it stands". The Imbue Studio app sends this when it enters a new workspace. A chat list window that is already showing a chat stays on that chat. If no chat list window is open, the pinned one comes up.

- With nothing signed in, the chat list opens the chooser even before any chat exists, once per page load. The first chat it shows after that counts as covered by that chooser: it stays open through the welcome chat's arrival, with any sign-in still in progress, and no second chooser opens. If the user closed it, it stays closed. While nothing is signed in, the empty list says "Connect an AI provider to start chatting." and has a "Choose a provider" button.

- If a sign-in finishes while no waiting chat is on screen, the first waiting chat the list shows afterwards (the welcome chat) is started on the new account.

- While nothing is signed in, an empty chat list no longer creates an empty "Chat 1", so the welcome chat lands on its own. With an account signed in, nothing changes.

- The chat list tells the Imbue Studio app once per page load, with `minds:provider-chooser-on-screen {}`, when its chooser is drawn with its providers listed in a window the user can see.
