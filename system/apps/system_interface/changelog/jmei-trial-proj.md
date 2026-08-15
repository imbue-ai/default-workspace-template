Honor `?open_app=<name>` deep links in the workspace shell.

The minds desktop client's new cross-workspace app selector navigates to the workspace shell with `?open_app=<name>` (through the `/goto/<host-id>/` cookie bridge). After the initial layout mounts, the shell focuses the app's existing tab if one is already open, or opens a new tab for it otherwise.

An app that has not registered yet is waited for rather than skipped: on a workspace that is still starting up, services come up staggered behind the interface itself, so a deep link routinely arrives before the app it names. Only if the app is still missing after five seconds is the link reported as unopenable, so a slow-starting app now opens instead of silently doing nothing. Naming the workspace interface itself is refused, since it cannot be opened as a tab inside itself.

The tab is opened through the same internal path as the "+" menu and agent-driven `layout.py open`, so a deep-linked app behaves identically however it was opened. `?open_app=terminal` therefore now allocates a real terminal session instead of mounting a session-less one.
