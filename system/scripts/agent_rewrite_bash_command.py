#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.agent_rewrite_bash_command`.

Kept as a stub: the harness hook configs (claude's, codex's, pi's extension and agy's shim)
run this path, and it puts oom_priority's source on sys.path, which only an entry file may do.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "services" / "oom_priority" / "src")
)

from workspace_bare_scripts.agent_rewrite_bash_command import main

if __name__ == "__main__":
    main()
