Notifications on a phone: the workspace can now push a notification to the owner's phone (or any browser on a share link) with the Imbue Studio app closed.

- A new page, `/notifications`, reached from the launcher's "Notifications on this device" row, turns Web Push on or off for the device it is opened on, sends it a test, lists every device that has it on, and shows the push service's raw answer. The row is offered only where the shell is the top-level page (a home-screen app, a browser on a share), never inside the Imbue Studio app. On iPhone and iPad it works from the home-screen app, iOS 16.4 or later.

- The shell serves a service worker at `/sw.js` that shows each pushed notification and, on a tap, opens the page it names (a chat, for the agents' notifications). It handles no fetches.

- The shell keeps a VAPID signing key and the owner's devices under `data/.state/system_interface/web_push/`. Only the owner registers, lists, removes or tests a device.

- `POST /api/web-push/send` (loopback only, refused through a share and in a preview) pushes `{title, body, path, tag}` to every registered device at once and answers each push service's status. A notification with the same tag replaces the earlier one on a device and still alerts; one too large for a push service is refused with a 400; a device whose subscription is gone is forgotten.
