#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.agent_memory_change_log`.

Kept as a stub: claude's hook config runs this path, and it puts agent_memory's and
chat_background_tasks' sources on sys.path, which only an entry file may do.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys
from pathlib import Path

# import-path: no-venv -- beyond the standard library it imports only `workspace_bare_scripts` and, through it, the stdlib-only `agent_memory.changes` and `chat_background_tasks`
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "agent_memory" / "src")
)
# import-path: no-venv -- beyond the standard library it imports only `workspace_bare_scripts` and, through it, the stdlib-only `agent_memory.changes` and `chat_background_tasks`
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "libs" / "chat_background_tasks" / "src"),
)

from workspace_bare_scripts.agent_memory_change_log import main

if __name__ == "__main__":
    raise SystemExit(main())
