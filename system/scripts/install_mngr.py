#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.install_mngr`.

Kept as a stub: the build runs it before any venv exists, and its module imports other
modules of its package, which the system python3 can reach only from this directory.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys

from workspace_bare_scripts.install_mngr import main

if __name__ == "__main__":
    sys.exit(main())
