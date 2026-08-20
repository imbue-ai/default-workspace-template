Renamed `auto_dismiss_dialogs` to `auto_dismiss_dialogs_at_startup` in the agent-config registry's inheritance documentation and the config loader's examples, following the rename in the plugins that declare the field. Same behaviour; the name now says when it applies.

Fixed the agent pane's bottom row being undisplayable. When re-fitting the manual-pinned agent window to an attaching client, the fit hook sized the window to the raw `#{client_height}`, which counts the tmux status line the window cannot use; since tmux does not clamp a manual-pinned window, the window ended up one row taller than the client could display, so whatever the agent drew on its bottom row rendered where nobody could see it. The hook now subtracts the status line's height (handling `on`/`off`/an explicit row count, per-session or global) after applying the minimum-client floor.

A TUI harness can now express readiness as a predicate over the pane, alongside the existing substring and regular-expression forms, for harnesses where readiness depends on *where* something appears rather than on whether it appears at all. The two existing forms are unchanged.

A readiness timeout no longer includes the pane's contents in the raised error. The pane is still logged; it is unbounded and can contain the user's own code or a diff, which does not belong in a surfaced message.

The TUI-ready timeout is now a public constant, so a harness can wait the same window the readiness check does when it needs to decide whether a pane is merely slow to draw.
