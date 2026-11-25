The shell's recovery pages say what failed and ask an agent to fix it.

- A startup read the shell answers with an error now sends the page to the shell's recovery page (`/recovery/desktops`) instead of retrying it. That page reads the desktops again itself and shows the shell's own failure behind "See details", says so when the workspace is out of space (with "Ask an agent to free up space", which tells the agent to), offers Reload, and goes back to the desktop on its own once they load; it offers no "Report a bug" for now.

- The recovery page, served when the shell's interface needs to be rebuilt and when its desktops couldn't be loaded, offers "Ask an agent to fix this", which asks in the main chat and shows it. Until an ask fails, the command (behind `/assist`, carrying the button's message) and a working terminal session are behind "See details"; once one fails, the page says "You can start one yourself" and shows the command, Copy and the terminal in the open, keeping only the failure's reason behind "See details". Its launches go through `POST /api/recovery/launch`.

- A page load that fails shows a page of ours with Try again instead of raw JSON.
