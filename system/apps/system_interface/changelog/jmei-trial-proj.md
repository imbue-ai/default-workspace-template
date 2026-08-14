Honor `?open_app=<name>` deep links in the workspace shell.

The minds desktop client's new cross-workspace app selector navigates to the workspace shell with `?open_app=<name>` (through the `/goto/<host-id>/` cookie bridge). After the initial layout mounts, the shell now consumes that parameter exactly once: it strips it from the address bar, waits for the app registry to load, and then focuses the app's existing dockview tab if one is already open, or opens a new iframe tab for it otherwise. Unknown or unregistered app names no-op, and `system_interface` itself is refused so the shell cannot iframe itself.

The tab is opened through the same internal path as the "+" menu and agent-driven `layout.py open`, so a deep-linked app behaves identically however it was opened. `?open_app=terminal` therefore now allocates a real terminal session instead of mounting a session-less one.
