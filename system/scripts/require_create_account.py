#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.require_create_account`.

Kept as a stub: the mngr create hook in .mngr/settings.toml and Imbue Studio's update
dispatcher run this path.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import os
from pathlib import Path

from workspace_bare_scripts.require_create_account import main

if __name__ == "__main__":
    raise SystemExit(main(dict(os.environ), Path.cwd()))
