New built-in app, the Onboarding checklist (`onboarding-checklist`, port 8070): one small page listing the four things worth trying in a new workspace (start a second chat, open Studio on a phone, build an app, connect some data), with a check mark on the ones already done.

A click on an entry shows where that thing is, whether or not it is done: the chat list's New chat button with its bubble, the share panel with a bubble at its phone control, a card popping out of the agent's avatar, or a card under the permissions button. The two entries that need the Imbue Studio app to draw a bubble cannot be clicked where it does not.

The app is in the launcher and has no desktop shortcut. The desktop's tour opens it as the tour ends. It stores nothing: `GET /api/items` reads what is done from the `onboarding_activity` library and the app registry, and answers an item it could not read as not done.
