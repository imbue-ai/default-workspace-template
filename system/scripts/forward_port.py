#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.forward_port`.

Kept as a stub: Imbue Studio and minds_evals run this path, and so does the supervisord
program of every app, including the ones users' own apps were scaffolded with.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

from workspace_bare_scripts.forward_port import main

if __name__ == "__main__":
    main()
