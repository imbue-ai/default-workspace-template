The op route's request models, the client, desktop, and window ids, the wallpaper reference, and the client-activity report now come from the shared `workspace_layout` library, so the shell and its callers read one definition. Every argument is read through those models, so a value off its rule is answered with a 400: a client, desktop, or window id as before, and now also a `client` or `desktop` that is not a string (ignored before) and an empty `app` or `path` (read as absent before).

- The op route's hints for an unknown or unsettled client name `workspace-layout context` instead of the removed `layout.py`.

- The op route parses the op into a `LayoutOp` once, and the dispatcher and its window and shortcut handlers match on it exhaustively, so an op added to the enum but not handled fails type checking instead of reaching a runtime "no handler" error.

- The shell's records (windows, desktops, placements, frames, cells, client records) and every answer it gives (the op answers, the inventory, the desktops and clients listings, the `context` answer, and the `layout_op` message) are `workspace_layout` models the shell builds its JSON from, rather than dicts it writes by hand; the JSON is unchanged but for timestamps, which now end in `Z`.

- The op route's `place` takes `state` (`SNAPPED_LEFT`, `SNAPPED_RIGHT`, `MAXIMIZED`) or a `frame` record; `zone` and a string `frame` are refused with a 400, as is a shortcut op's string `cell`. `context` is answered by the same dispatcher as every other op.
