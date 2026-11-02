#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.provision_backups`.

Kept as a stub: the web client's create flow runs this path through owner-exec.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

from workspace_bare_scripts.provision_backups import main

if __name__ == "__main__":
    main()
