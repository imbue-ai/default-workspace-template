#!/usr/bin/env python3
"""Entry point for :mod:`update_self_skill.update_self`.

The guard below runs before anything is imported and parses on any Python 3: a workspace
whose system python3 is older than the 3.12 floor (a local Lima workspace from before
2026-09-14, on Debian 12) cannot run this release's update code, so the staged copy exits
with its own code and prints what the update pass does instead, which only such a
workspace's lead needs to read.
"""

import sys
from pathlib import Path

# Kept as a stub: every release's update-self flow and Imbue Studio's update dispatcher run
# this path with the system python3 and no venv, in place and from the target release's
# staged copy, so the package beside scripts/ is put on sys.path here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

UPDATE_IMPOSSIBLE_EXIT_CODE = 75

if sys.version_info < (3, 12):
    sys.stderr.write(
        "update-self: this workspace's system Python is {}.{}, older than the 3.12 this "
        "update needs, so it cannot be updated in place. Stop the update pass here: apply "
        "nothing and dispatch no worker. Then:\n"
        "1. Record the verdict with the workspace's own copy of update-self (this copy "
        "refuses every subcommand under this Python):\n"
        "   python3 .agents/skills/update-self/scripts/update_self.py run-status verdict "
        'NEEDS_RECREATION --detail "System Python too old to update in place."\n'
        "   If that copy rejects run-status as an invalid choice, it predates minds-v0.4.3 "
        "and recorded no run, so skip this.\n"
        '2. Tell the user as "Migration-required updates" in {} says: the update needs a '
        "new workspace, and the migrate-workspace skill moves their work to it.\n"
        "3. Release the updating-workspace lease: "
        'tk close "$UPDATE_LEASE_ID" "Update pass finished."\n'
        "There is no history bridge or tracking ticket to clean up yet.\n".format(
            sys.version_info[0],
            sys.version_info[1],
            Path(__file__).resolve().parents[1] / "references" / "apply-outcomes.md",
        )
    )
    sys.exit(UPDATE_IMPOSSIBLE_EXIT_CODE)

from update_self_skill.update_banding import protect_from_memory_shed
from update_self_skill.update_self import main, shed_protection_target

if __name__ == "__main__":
    _banding_root = shed_protection_target(sys.argv[1:])
    if _banding_root is not None:
        protect_from_memory_shed(_banding_root.resolve())
    sys.exit(main())
