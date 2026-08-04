Fixed two ways the workspace interface could be left showing nothing after an update, with no way for you to recover it.

If the compiled interface goes missing, the page you get now offers to rebuild it for you, shows the rebuild's progress, and reloads into the working interface when it finishes. Previously it printed a command that you were not in a position to run, so a workspace in that state stayed broken until someone with a terminal fixed it. When the interface's sources are not present at all, the page says so plainly instead of offering a repair it cannot perform.

Fixed a blank screen that appeared if the interface was rebuilt while the workspace server was running. The server used to decide whether to serve the interface's scripts once, at startup, based on whether a build existed at that moment -- so a build that appeared later was never served, and the browser silently refused to run the page. The scripts are now always served, and a genuinely missing one returns a plain "not found" instead of the page itself.

The server also now records a warning naming the exact directory it looked in whenever it falls back to the "not built" page, so the cause is visible in the service logs rather than only on screen.
