#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.background_tasks`."""

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "libs" / "chat_background_tasks" / "src"),
)

from workspace_bare_scripts.background_tasks import main

if __name__ == "__main__":
    raise SystemExit(main())
