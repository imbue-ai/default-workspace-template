#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.with_secrets`.

Kept as a stub: app launch lines run this path, including the supervisord programs
users' own apps were scaffolded with.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys

from workspace_bare_scripts.with_secrets import main

if __name__ == "__main__":
    sys.exit(main())
