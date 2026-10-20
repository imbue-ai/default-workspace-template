#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.agent_rewrite_bash_command`."""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "services" / "oom_priority" / "src")
)

from workspace_bare_scripts.agent_rewrite_bash_command import main

if __name__ == "__main__":
    main()
