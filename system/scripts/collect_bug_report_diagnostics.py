#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.collect_bug_report_diagnostics`.

Kept as a stub: Imbue Studio's bug report runs this path in templates of every version.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys

from workspace_bare_scripts.collect_bug_report_diagnostics import main

if __name__ == "__main__":
    main(sys.argv[1:])
