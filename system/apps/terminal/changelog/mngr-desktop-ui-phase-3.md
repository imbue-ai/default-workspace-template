The terminal is now two programs. `terminal` serves a wrapper page on the terminal origin: `/?session=<name>` frames the session's ttyd page from the new internal `terminal-pty` origin, reports its path and the session's title to the shell, re-points the frame when the shell asks it to navigate, and passes the shell's focus grant on to ttyd. `/new[?workdir=]` allocates the lowest free `terminal-N`, creates its tmux session, and redirects to its page. The instances API and the tmux hook route stay on 7682.

`terminal-pty` (the `terminal-pty` entry point of the same package, ttyd on port 7683) installs the dispatch scripts and the patched web client, registers `system/apps/terminal_pty/app.toml`, and becomes ttyd.

A terminal's instance URL is now `/?session=<name>&tab={tab}`; the ttyd argument URL is the wrapper's business.
