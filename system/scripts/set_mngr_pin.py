#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.set_mngr_pin`.

Kept as a stub: the mngr repo's pin scripts run this path against template checkouts of
any version.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys

from workspace_bare_scripts.set_mngr_pin import main

if __name__ == "__main__":
    sys.exit(main())
