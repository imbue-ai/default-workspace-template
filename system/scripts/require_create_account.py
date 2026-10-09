#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.require_create_account`."""

import os
from pathlib import Path

from workspace_bare_scripts.require_create_account import main

if __name__ == "__main__":
    raise SystemExit(main(dict(os.environ), Path.cwd()))
