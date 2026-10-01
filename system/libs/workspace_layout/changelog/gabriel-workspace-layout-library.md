New library `workspace_layout`: the shell's layout routes (the op route, the desktops, clients, and client-activity routes, and where the shell listens) as one typed client that the chat, Getting Started, the browser, the terminal, and the shell itself share.

- `ShellLayoutClient` asks the shell to `show`, `open`, `focus`, `navigate`, `place`, `close`, and `refresh`, lists its connected clients and desktops, and reports client activity; every op names the client it targets and raises a typed error when the shell is unreachable, refuses, or answers something else.

- The op route's request models (`DesktopOpArguments`, `OpRequester`, and `LayoutOp`, the enum of every op), the ids the wire carries, and the window readers an app with window-bound resources sweeps against now live here, so the shell and its callers read one definition.

- `FakeShell` and `LoopbackShell` are the shared test stand-ins; `LoopbackShell` refuses any op body the shell itself would refuse.

- The agent-facing desktop command is now `uv run workspace-layout`, this library's console script, replacing `python3 system/scripts/layout.py`. Every subcommand, output line, and exit code is unchanged, and every flag but `place --zone`, which became `--state` (the hints for retired verbs and spellings name the new command), and a new `show <app> --path P [--showing P ...] [--repoint PAGE ...]` runs the shell's `show` op: it raises a window already showing the page rather than opening another.

- The shell's records and answers are defined here and nowhere else: `records` (frames, cells, windows, desktops, placements, layouts, client records, and the views answers carry), every answer the shell gives (`DesktopOpAnswer` with its desktop and layout, `ShowAnswer.shown` as a `ShowOutcome`, the transient, `context`, inventory, desktops, and clients answers, and the `layout_op` message), and the wire's values (`WindowPath`, `WindowPage`, `WindowTitle`, `UserId`, `WindowState`, `SpecialWindow`). The shell builds from them and callers parse with them, ignoring fields a newer shell adds.

- The op route's arguments are typed: `place` takes a `state` (`SNAPPED_LEFT`, `SNAPPED_RIGHT`, `MAXIMIZED`) or a `frame` record instead of a `zone` or an `x,y,width,height` string, a shortcut's `cell` is a `{column, row}` record, and paths and pages are checked as the shell checks them before anything is posted. `OpBody`, `parse_op_body`, `read_op_arguments`, and `parse_window_reference` read a body as the shell does.

- `workspace-layout place` takes `--state snapped-left|snapped-right|maximized`; `--zone` is refused with that form. Every subcommand reads the shell's answer through the answer models rather than picking keys out of it.

- A property that holds for some ops (`op_reads_arguments`, the shell's window and shortcut handlers) is an exhaustive match over `LayoutOp` rather than a set of op names, so a new op fails type checking until each says what it does for it.

- `WindowClosedHint` and `parse_window_closed_hint` are the body the shell posts when a window closes.
