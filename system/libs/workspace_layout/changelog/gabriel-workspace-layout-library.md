New library `workspace_layout`: the shell's layout routes (the op route, the desktops, clients, and client-activity routes, and where the shell listens) as one typed client that the chat, Getting Started, the browser, the terminal, and the shell itself share.

- `ShellLayoutClient` asks the shell to `show`, `open`, `focus`, `navigate`, `place`, `close`, and `refresh`, lists its connected clients and desktops, and reports client activity; every op names the client it targets and raises a typed error when the shell is unreachable, refuses, or answers something else.

- The op route's request models (`DesktopOpArguments`, `OpRequester`, the op names), the ids the wire carries, and the window readers an app with window-bound resources sweeps against now live here, so the shell and its callers read one definition.

- `FakeShell` and `LoopbackShell` are the shared test stand-ins; `LoopbackShell` refuses any op body the shell itself would refuse.
