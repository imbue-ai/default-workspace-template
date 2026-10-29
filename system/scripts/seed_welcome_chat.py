#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.seed_welcome_chat`.

Kept as a stub: Imbue Studio runs this path through ``mngr exec``.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

from workspace_bare_scripts.seed_welcome_chat import main

if __name__ == "__main__":
    raise SystemExit(main())
