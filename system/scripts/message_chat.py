#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.message_chat`.

Kept as a stub: Imbue Studio runs this path, and so does update-self's runner from the
target release's staged copy, on whichever tree it lands on.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

from workspace_bare_scripts.message_chat import main

if __name__ == "__main__":
    raise SystemExit(main())
