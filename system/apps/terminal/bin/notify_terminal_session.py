#!/usr/bin/env python3
"""A stand-in for the tmux hook poster the tabbed shell's terminal app used.

A running tmux server keeps the hook commands it read at start, and the update flow restarts
supervisord programs, not the shared tmux server the agents live in: a server started on a
release whose ``terminal_tmux.conf`` set ``client-session-changed`` and ``session-renamed``
hooks still runs ``python3 <this path> ...`` on every client session switch and rename, and
``run-shell`` shows a failing command's output in the user's terminal. This exits quietly
instead; the desktop has nothing to tell.
"""

import sys

# CLEANUP: delete this file (and its ``bin/`` directory) once every workspace's tmux server has
# restarted on a release without the hook lines (a container restart is what restarts the server).
if __name__ == "__main__":
    sys.exit(0)
