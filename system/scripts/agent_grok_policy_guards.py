#!/usr/bin/env python3
"""Entry point for :mod:`workspace_bare_scripts.agent_grok_policy_guards`.

Kept as a stub: grok's hook config runs this path, and it puts oom_priority's source on sys.path,
which only an entry file may do.

The system python3 finds workspace_bare_scripts only because this file's directory is first
on sys.path, so run it as ``python3 <path>``, never with -I, -P or PYTHONSAFEPATH set.
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "services" / "oom_priority" / "src")
)

from workspace_bare_scripts.agent_grok_policy_guards import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr, os.environ))
