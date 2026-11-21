On a new workspace with no AI account signed in, the provider chooser now opens as soon as the chat list is on screen. It no longer waits for the welcome chat to be seeded or for the chat app's first read of the agent list.

- The provider chooser that a chat with nothing signed in asks for now belongs to the chat list rather than to the chat's page. The chat list opens it over everything it shows, and the chat's "Choose a provider" button opens the same one. It stays open when the chat on screen changes, so a sign-in already under way is kept when the welcome chat arrives, and no second chooser opens behind it. A chat page opened on its own, outside the chat list, still opens its own chooser.

- With nothing signed in, the chat list opens the chooser even before any chat exists, once per page load. The first chat it shows after that counts as covered by that chooser; if the user closed it, it stays closed. While nothing is signed in, the empty list says "Connect an AI provider to start chatting." and has a "Choose a provider" button, and it no longer creates an empty "Chat 1", so the welcome chat lands on its own. With an account signed in, nothing changes.

- A sign-in launches the chat waiting on screen when it finishes. If no waiting chat is on screen, it launches the first waiting chat the list shows afterwards (the welcome chat).

- `POST /api/focus-chat` takes a missing `chatId` to mean "show the chat list as it stands". The Imbue Studio app sends this when it enters a new workspace. A chat list window already showing a chat stays on it; if none is open, the pinned one comes up.

- The chat list tells the Imbue Studio app once per page load, with `minds:provider-chooser-on-screen {}`, when its chooser is drawn with its providers listed in a window the user can see.
