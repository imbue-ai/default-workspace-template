#!/usr/bin/env python3
"""Entry point for :mod:`update_self_skill.update_self`."""

import sys

from update_self_skill.update_banding import protect_from_memory_shed
from update_self_skill.update_self import main, shed_protection_target

if __name__ == "__main__":
    _banding_root = shed_protection_target(sys.argv[1:])
    if _banding_root is not None:
        protect_from_memory_shed(_banding_root.resolve())
    sys.exit(main())
