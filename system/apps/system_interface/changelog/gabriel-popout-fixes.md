Three fixes for a window pulled out into its own Imbue Studio window:

- An agent's `refresh <window>` now reloads the pulled-out window's page (and the main window's hidden copy of it). The pulled-out window's page now registers its connection with the shell under its client, marked as a pop-out's, so the ops aimed at the client reach it; the registration counts toward the client being connected, and never reports an active desktop, so the client's desktop stays its main window's.

- Reloading the interface (`refresh_workspace_view.py`, an update) no longer turns a pulled-out window into a whole desktop: its page keeps `?solo=<window>` in its URL, so any reload comes back as the same pulled-out window.

- A pulled-out window Imbue Studio reopens at launch (a restored session, a reopen of the app, a backend retry), whose window was brought back to the desktop while the app was away, now closes instead of pulling the window out again. Studio marks such a window `reopened=1` next to `solo`; only a freshly torn-out one (or one from an older Studio that sends no mark) waits the grace for the desktop's save before marking the window out itself.
