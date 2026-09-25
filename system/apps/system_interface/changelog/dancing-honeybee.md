**`open` takes a `beside` argument.** It names a window (a window id, `self`,
`pinned`, or an app name, resolved the way the window verbs resolve one) that
the opened window should sit next to: the named window is set `SNAPPED_LEFT`
and the opened one `SNAPPED_RIGHT` and on top of the stack, for the target
client alone. Both keep their own frames, so `restore` returns either to where
it stood. A `beside` that matches no window on the desktop -- a chat the user
closed, a requester that is nobody's chat -- leaves the opened window where it
would have landed rather than refusing the open: the pairing is the open's
courtesy, not its point.

This is the desktop's answer to what the tabbed shell did when an agent opened
something it had just built: dock it beside the chat that asked for it, rather
than over the conversation the user is reading.
