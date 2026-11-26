#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.backup_lease`.

Kept as a stub: the Imbue Studio desktop's backup programs run this path through ``mngr exec``.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

from workspace_bare_scripts.backup_lease import main

if __name__ == "__main__":
    raise SystemExit(main())
