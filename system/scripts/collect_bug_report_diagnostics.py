#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.collect_bug_report_diagnostics`."""

import sys

from workspace_bare_scripts.collect_bug_report_diagnostics import main

if __name__ == "__main__":
    main(sys.argv[1:])
