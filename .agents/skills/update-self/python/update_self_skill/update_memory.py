"""What an update could stop to free memory, as the workspace tree's own memory tool prints it.

The tool reads what the running workspace wrote: mngr's listing, the agent-pid registry, the
shell's and the Screen service's answers, the memory guard's program file. Only the copy in the
workspace's own tree is sure to match those, so that is the copy run here, never one from the
target release, and what it prints goes to the lead as it is.

Stdlib-only, like its siblings here.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# own-process: other-tree -- the memory tool reads what the running workspace wrote, so the copy in that workspace's tree is the one that matches it
WORKSPACE_MEMORY_REL = (
    Path("system") / "services" / "oom_priority" / "bin" / "workspace_memory.py"
)
WORKSPACE_MEMORY_ARGS = ("list", "--stoppable")

# CLEANUP: drop the earlier tool's path and both notes, and leave print_memory running
# workspace_memory.py alone, once update-self's floor, the oldest release the app updates from,
# reaches the first release that ships workspace_memory.py: only a workspace older than that
# lacks it.
MEMORY_CANDIDATES_REL = (
    Path("system") / "services" / "oom_priority" / "bin" / "memory_candidates.py"
)
_EARLIER_TOOL_NOTE = """
This workspace's version has the earlier memory tool, which lists the same things by kind. An app
is stopped by its PROGRAM, and AUTO-STOP `no` is an app that keeps running with no window. What it
does not print is unknown, not absent: it lists no apps before minds-v0.9.2, and before
minds-v0.10.7 does not say how much free memory is left before the memory guard starts stopping
processes.
"""
_NO_TOOL_NOTE = (
    "Free memory: unknown\n"
    "This workspace's version has no memory listing, so only chats can be stopped "
    "(`run-status stop-chats --agent`); say so in the results message.\n"
)


def print_memory(repo_root: Path) -> int:
    """Run the workspace tree's memory tool, its output going straight to stdout, and return its
    exit code."""
    current = repo_root / WORKSPACE_MEMORY_REL
    if current.is_file():
        return _run(current, WORKSPACE_MEMORY_ARGS, repo_root)
    earlier = repo_root / MEMORY_CANDIDATES_REL
    if earlier.is_file():
        returncode = _run(earlier, (), repo_root)
        sys.stdout.write(_EARLIER_TOOL_NOTE)
        return returncode
    sys.stdout.write(_NO_TOOL_NOTE)
    return 0


def _run(tool: Path, args: Sequence[str], repo_root: Path) -> int:
    return subprocess.run(
        [sys.executable, str(tool), *args], cwd=repo_root, check=False
    ).returncode
