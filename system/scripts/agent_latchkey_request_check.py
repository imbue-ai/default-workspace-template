#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.agent_latchkey_request_check`."""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

from workspace_bare_scripts.agent_latchkey_request_check import main

if __name__ == "__main__":
    sys.exit(main())
