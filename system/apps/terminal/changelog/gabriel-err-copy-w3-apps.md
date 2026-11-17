Terminal windows now say why a terminal ended or could not open, instead of "Connection Closed" or a JSON body.

- A terminal tmux cannot start holds on "This terminal couldn't start. Try again. If this persists, report a bug." with Try again, "Ask an agent to fix this" (a new chat asking an agent to fix it, with tmux's text), Report a bug, and tmux's own text behind "See details". One that closed with an error, or whose folder is gone, says "This terminal closed." with the cause where known and a Start a new terminal button. Exiting the shell yourself still offers to reconnect.

- A terminal window whose page cannot load shows a page of its own: a name no terminal has, terminals not responding (with Try again only, since agents run in the same terminal server), saved terminals that cannot be read or were written by a newer version ("..., so terminals can't open.", with "Ask an agent to fix this"), or any other failure (with Try again and Report a bug), the raw reason behind "See details".

- After a press, "Ask an agent to fix this" reads "Asking an agent…" and is disabled until the new chat's window opens; if it can't open, the button comes back with the reason under it and its details behind "See details".

- The agent and Report a bug buttons appear only inside the workspace's desktop, and Report a bug only where Imbue Studio can open its bug report. A bug report carries the page's sentence and what its "See details" holds.

- The test suite declares `app-manifest[testing]` (pytest and playwright for the shared shell stand-in helpers) in its dev dependencies.
