"""Tests for the system interface's one subprocess entry point.

The property that matters is a real kernel one, so these run a real child and read the ids
back out of it rather than asserting on how the runner was called.
"""

from __future__ import annotations

import os
import sys

from imbue.system_interface.subprocess_runner import run_detached_command

_REPORT_IDS = "import os; print(os.getsid(0), os.getpgid(0))"


def test_the_child_runs_in_its_own_session_and_process_group() -> None:
    """No inherited session means no inherited controlling terminal, which is the whole point:
    a child with no terminal cannot stop this process by touching one."""
    finished = run_detached_command([sys.executable, "-c", _REPORT_IDS], timeout=30.0)

    assert finished.returncode == 0, finished.stderr
    child_session_id, child_process_group_id = (int(field) for field in finished.stdout.split())
    assert child_session_id != os.getsid(0)
    assert child_process_group_id != os.getpgid(0)
    assert child_session_id == child_process_group_id


def test_a_failing_command_is_reported_rather_than_raised() -> None:
    finished = run_detached_command([sys.executable, "-c", "raise SystemExit(7)"], timeout=30.0)

    assert finished.returncode == 7
