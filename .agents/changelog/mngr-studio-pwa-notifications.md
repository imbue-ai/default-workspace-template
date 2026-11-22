- `notify-user` also asks the workspace to notify the owner's phone (or any browser the owner turned notifications on for), but only from a chat no one else wrote in, and not while the owner is reading it. The notification names the chat; the agent's words show only if the owner turned full text on. The script exits 0 when the Imbue Studio app took the notification or the workspace queued it for a device; a push that was not sent is a warning, not a failure.

- `use-from-phone` gains an optional fourth step: turning on notifications for the phone, and what to check when none arrive.
