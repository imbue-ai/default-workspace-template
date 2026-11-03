#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.wait_for_critical_path`.

Kept as a stub: supervisord runs it under the system python3, which finds
workspace_bare_scripts only because this file's directory is first on sys.path, so run it as
``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

from workspace_bare_scripts.wait_for_critical_path import main

if __name__ == "__main__":
    main()
