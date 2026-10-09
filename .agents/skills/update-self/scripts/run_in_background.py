#!/usr/bin/env python3
"""Entry point for :mod:`update_self_skill.run_in_background`."""

import sys
from pathlib import Path

# Run by the system python3 with no venv, in place and from update-self's staged copy, so
# the package beside scripts/ is put on sys.path here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from update_self_skill.run_in_background import main

if __name__ == "__main__":
    raise SystemExit(main(Path(__file__).resolve()))
