#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.claude_background_task_hooks`.

Kept as a stub: claude's hook config runs this path, and it puts oom_priority's and
chat_background_tasks' sources on sys.path, which only an entry file may do.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "services" / "oom_priority" / "src")
)
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "libs" / "chat_background_tasks" / "src"),
)

from workspace_bare_scripts.claude_background_task_hooks import main

if __name__ == "__main__":
    raise SystemExit(main())
