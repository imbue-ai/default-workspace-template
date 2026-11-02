#!/usr/bin/env python3
"""Entry point for :mod:`update_self_skill.run_in_background`."""

import sys
from pathlib import Path

# Kept as a stub: every release's update-self flow runs this path from the target release's
# staged copy with the system python3 and no venv, so the package beside scripts/ is put on
# sys.path here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from update_self_skill.run_in_background import main

if __name__ == "__main__":
    raise SystemExit(main(Path(__file__).resolve()))
