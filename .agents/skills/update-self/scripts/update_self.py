#!/usr/bin/env python3
"""Entry point for :mod:`update_self_skill.update_self`.

The guard below runs before anything is imported and parses on any Python 3: a workspace
whose system python3 is older than the 3.12 floor (a local Lima workspace from before
2026-09-14, on Debian 12) cannot run this release's update code, so the staged copy says so
and exits with its own code instead of failing somewhere inside the update.
"""

import sys

UPDATE_IMPOSSIBLE_EXIT_CODE = 75

if sys.version_info < (3, 12):
    sys.stderr.write(
        "update-self: this workspace's system Python is {}.{}, older than the 3.12 this "
        "update needs, so it cannot be updated in place. Move its work to a new workspace "
        "with the migrate-workspace skill instead.\n".format(*sys.version_info[:2])
    )
    sys.exit(UPDATE_IMPOSSIBLE_EXIT_CODE)

from update_self_skill.update_banding import protect_from_memory_shed
from update_self_skill.update_self import main, shed_protection_target

if __name__ == "__main__":
    _banding_root = shed_protection_target(sys.argv[1:])
    if _banding_root is not None:
        protect_from_memory_shed(_banding_root.resolve())
    sys.exit(main())
