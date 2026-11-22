Notifications on a phone: the workspace can push a notification to the owner's phone (or a browser on a share link) when one of the owner's chats has something for them, with the Imbue Studio app closed.

- A new page, `/notifications`, turns Web Push on or off for the device it is opened on, sends it a test, and lists every device with when it last received one. The launcher's "Notifications on this device" row reaches it, offered only to the owner and only outside the Imbue Studio app's frame: in the menu on a phone or tablet, by search on a computer, where the page asks for an explicit "Turn on in this browser anyway". On iPhone and iPad it works from the home-screen app, iOS 16.4 or later.

- A notification names the chat it came from and, unless the owner turns on "Show what the agent wrote", nothing the agent said, so nothing private shows on a locked screen. A tap opens the chat.

- Only a chat's `notify-user` can send: the loopback `POST /api/web-push/send` needs the shell's send token, which app code is never given, and refuses anything through a share or in a preview. It answers at once and posts on its own thread, folds a burst from one chat into one notification, and drops sends past 4 per chat in 10 minutes or 20 per hour.

- The page shows the owner the recent sends (which chat, what was asked, what was shown, each device's outcome) and a reset that replaces the signing key and send token and turns notifications off on every device. A device whose push service refuses the key is marked as needing to turn notifications on again rather than listed as working.
