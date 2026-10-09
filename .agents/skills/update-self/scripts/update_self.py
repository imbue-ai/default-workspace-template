#!/usr/bin/env python3
"""Entry point for :mod:`update_self_skill.update_self`."""

import sys
from pathlib import Path

# Run by the system python3 with no venv, in place and from update-self's staged copy, so
# the package beside scripts/ is put on sys.path here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from update_self_skill.update_banding import protect_from_memory_shed
from update_self_skill.update_self import main, shed_protection_target

if __name__ == "__main__":
    _banding_root = shed_protection_target(sys.argv[1:])
    if _banding_root is not None:
        protect_from_memory_shed(_banding_root.resolve())
    sys.exit(main())
