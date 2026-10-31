#!/usr/bin/env python3
"""Entry point for :mod:`workspace_scripts.refresh_workspace_view`.

Kept as a stub: update-self runs this path on whichever tree it lands on, including an
older release's after a rollback, where only this path exists.
"""

import sys

from workspace_scripts.refresh_workspace_view import main

if __name__ == "__main__":
    sys.exit(main())
