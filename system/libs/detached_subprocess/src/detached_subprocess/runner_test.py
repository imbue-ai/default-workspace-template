"""Tests for the one subprocess entry point a workspace service shells out through.

The property that matters is a real kernel one, so these run a real child and read the ids
back out of it rather than asserting on how the runner was called.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from detached_subprocess.runner import (
    DetachedSpawnError,
    run_detached_command,
    run_detached_subprocess,
    spawn_detached_process,
)

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


def test_the_stdlib_shaped_runner_also_gets_its_own_session() -> None:
    """``run_detached_subprocess`` is a separate call into subprocess, so its detachment needs
    proving on its own rather than by resemblance to ``run_detached_command``."""
    completed = run_detached_subprocess([sys.executable, "-c", _REPORT_IDS], timeout=30.0)

    assert completed.returncode == 0, completed.stderr
    child_session_id, child_process_group_id = (int(field) for field in completed.stdout.split())
    assert child_session_id != os.getsid(0)
    assert child_session_id == child_process_group_id


def test_a_long_lived_child_runs_in_its_own_session() -> None:
    process = spawn_detached_process([sys.executable, "-c", _REPORT_IDS], stdout=subprocess.PIPE)
    try:
        stdout, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0
    child_session_id, child_process_group_id = (int(field) for field in stdout.split())
    assert child_session_id != os.getsid(0)
    assert child_session_id == child_process_group_id


def test_a_long_lived_child_that_cannot_start_is_reported() -> None:
    with pytest.raises(DetachedSpawnError):
        spawn_detached_process(["definitely-not-a-real-binary-xyz"])
