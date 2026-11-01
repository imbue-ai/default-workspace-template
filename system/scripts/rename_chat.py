#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.rename_chat`.

Kept as a stub: the script runs with no venv (the build-app skill runs this path with the
system python3) and imports its package's message_chat module.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

from workspace_bare_scripts.rename_chat import main

if __name__ == "__main__":
    raise SystemExit(main())
