#!/usr/bin/env python3
"""Entry point for :mod:`app_registration.forward_port`.

Kept as a stub: Imbue Studio and minds_evals run this path, and so does the supervisord
program of every app, including the ones users' own apps were scaffolded with. It puts
app_registration's source on sys.path, which only an entry file may do.
"""

import sys
from pathlib import Path

# import-path: no-venv -- the system python3 runs this with no venv, so it finds its stdlib-only packages by path
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "app_registration" / "src")
)

from app_registration.forward_port import main

if __name__ == "__main__":
    main()
