#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.background_tasks`.

Kept as a stub: Imbue Studio's workspace programs, minds_evals and minds_admin run this path
in templates of every version, and it puts chat_background_tasks' source on sys.path.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "libs" / "chat_background_tasks" / "src"),
)

from workspace_bare_scripts.background_tasks import main

if __name__ == "__main__":
    raise SystemExit(main())
