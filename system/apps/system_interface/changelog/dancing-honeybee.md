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

**A window travels to its new rectangle instead of appearing at it.** Any move
the pointer did not make -- a snap from the size menu, a restore, a `place`, the
pairing above -- now runs as a 180ms transition (`--desk-window-move`). Only the
chrome transitions: its live page is positioned by measuring the chrome, so it
has nothing to transition towards and is re-measured every frame of the travel
instead. A press sets `data-window-motion="off"` on the desktop's root for its
whole length, so a dragged window stays under the pointer and the settle at the
release does not animate; `prefers-reduced-motion` turns the travel off entirely.
