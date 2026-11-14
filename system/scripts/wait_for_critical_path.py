#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.wait_for_critical_path`.

Kept as a stub: supervisord runs this path under the system python3, including from the
program blocks no release rewrites (the one the github-sync skill writes, and an installed
Datalib program), and it puts app_registration's source on sys.path, which only an entry file
may do.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys
from pathlib import Path

# import-path: no-venv -- beyond the standard library it imports only `workspace_bare_scripts` and, through it, the stdlib-only `app_registration`
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "app_registration" / "src")
)

from workspace_bare_scripts.wait_for_critical_path import main

if __name__ == "__main__":
    main()
