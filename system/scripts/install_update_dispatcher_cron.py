#!/usr/bin/env python3
"""Entry point for :func:`workspace_update_control.tick.install_cron_line_main`.

Kept as a stub: the workspace's boot runs this path with ``python3`` from the workspace root, naming the
directory cron reads its drop-ins from, to write the ``update-self-dispatch`` cron line that runs the update
dispatcher every minute. It puts workspace_update_control's source on sys.path, which only an entry file may do.
"""

import sys
from pathlib import Path

# import-path: no-venv -- the boot runs it before it re-syncs the venv, and it imports only the stdlib-only workspace_update_control
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "libs"
        / "workspace_update_control"
        / "src"
    ),
)

from workspace_update_control.tick import install_cron_line_main

if __name__ == "__main__":
    install_cron_line_main()
