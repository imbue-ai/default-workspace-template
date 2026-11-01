#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.vendor_mngr_skill`.

Kept as a stub: the mngr repo's pin-bump script runs this path.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys

from workspace_bare_scripts.vendor_mngr_skill import main

if __name__ == "__main__":
    sys.exit(main())
