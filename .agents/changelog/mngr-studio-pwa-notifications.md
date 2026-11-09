- `notify-user` also pushes the notification to every device the owner turned notifications on for (a phone with the workspace on its home screen, a browser), unless a page is already watching the chat. Tapping it opens the chat. The script now exits 0 when the Imbue Studio app or at least one device took the notification; with no device turned on nothing changes.

- `use-from-phone` gains an optional fourth step: turning on notifications for the phone, and what to check when none arrive.
