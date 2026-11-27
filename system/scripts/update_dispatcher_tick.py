#!/usr/bin/env python3
"""Entry point for :mod:`workspace_update_control.tick`.

Kept as a stub: the workspace's cron runs this path with ``python3`` every minute (the
``update-self-dispatch`` line the update routes write). It puts workspace_update_control's
source on sys.path, which only an entry file may do.
"""

import sys
from pathlib import Path

# import-path: no-venv -- cron runs the tick while an update may have broken the venv, and it imports only the stdlib-only workspace_update_control
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "libs"
        / "workspace_update_control"
        / "src"
    ),
)

from workspace_update_control.tick import main

if __name__ == "__main__":
    main()
