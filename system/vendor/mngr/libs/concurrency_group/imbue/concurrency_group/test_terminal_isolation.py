"""Integration tests: what a timed-out child can do to the process that ran it.

``run_local_command_modern_version`` kills a child that overruns its timeout. If that child
can reach a controlling terminal it inherited, the kill is not contained: restoring terminal
modes from a background process group makes the kernel deliver SIGTTOU to the *whole* group,
stopping the caller along with the child. That is how a workspace's system interface freezes
-- it runs under supervisord in a tmux pane, so it sits in a background process group on the
pane's terminal, and the ``claude`` CLI it spawns opens ``/dev/tty`` directly even when its
stdio is fully redirected.

Both tests drive the real thing through ``_terminal_freeze_test_script.py``: a real pty, a
real session leader holding it in the foreground, a real background process group, and a real
SIGTERM-on-timeout. The first pins the exposure that ``is_detached_from_terminal`` exists to
close; the second shows it closed, and shows *why* -- the child can no longer open the
terminal at all.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.concurrency_group import _terminal_freeze_test_script
from imbue.concurrency_group._terminal_freeze_test_script import _NO_TERMINAL_EXIT_CODE

_HARNESS_SCRIPT: Final[Path] = Path(_terminal_freeze_test_script.__file__)
# Comfortably above the harness's own 20s internal deadline, so a harness that misbehaves
# reports a verdict rather than being cut off here.
_HARNESS_TIMEOUT_SECONDS: Final[float] = 40.0


def _kill_service_group(verdict_path: Path) -> None:
    """Best-effort cleanup for a service left stopped by a harness that did not finish."""
    if not verdict_path.exists():
        return
    service_pid = json.loads(verdict_path.read_text(encoding="utf-8")).get("service_pid")
    if service_pid is None:
        return
    try:
        os.killpg(service_pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_harness(tmp_path: Path, is_detached_from_terminal: bool) -> dict[str, Any]:
    verdict_path = tmp_path / "verdict.json"
    marker_path = tmp_path / "toucher.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(_HARNESS_SCRIPT),
            "session",
            str(verdict_path),
            str(marker_path),
            "detached" if is_detached_from_terminal else "attached",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate(timeout=_HARNESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        process.kill()
        output, _ = process.communicate()
        _kill_service_group(verdict_path)
        raise AssertionError(f"harness did not finish within {_HARNESS_TIMEOUT_SECONDS}s; output:\n{output}") from e
    if process.returncode != 0:
        _kill_service_group(verdict_path)
        raise AssertionError(f"harness exited {process.returncode}; output:\n{output}")

    verdict: dict[str, Any] = json.loads(verdict_path.read_text(encoding="utf-8"))
    verdict["toucher"] = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None
    return verdict


def _assert_the_caller_was_backgrounded_on_a_terminal(verdict: dict[str, Any]) -> None:
    """Guard against a vacuous pass: without this shape there is no SIGTTOU to avoid."""
    assert verdict["foreground_process_group_id"] == verdict["own_process_group_id"], (
        f"harness did not hold the terminal's foreground group: {verdict}"
    )
    assert verdict["service_session_id"] == verdict["session_id"], (
        f"the caller is not in the terminal's session: {verdict}"
    )
    assert verdict["service_process_group_id"] != verdict["foreground_process_group_id"], (
        f"the caller is not in a background process group: {verdict}"
    )


@pytest.mark.timeout(60)
def test_a_terminal_touching_child_stops_the_whole_caller_group_when_its_timeout_kills_it(tmp_path: Path) -> None:
    """The default: a child inherits the caller's terminal, so killing it can freeze the caller.

    This is the workspace freeze, reduced. Every process in the group -- the caller and the
    child it was trying to kill -- ends up stopped, and the caller never returns from the call.
    """
    verdict = _run_harness(tmp_path, is_detached_from_terminal=False)
    _assert_the_caller_was_backgrounded_on_a_terminal(verdict)

    assert verdict["toucher"]["session_id"] == verdict["session_id"], (
        f"the child did not inherit the caller's session: {verdict}"
    )
    assert verdict["toucher"]["terminal"] == "reached", f"the child never got into tcsetattr: {verdict}"
    assert verdict["outcome"] == "stopped", f"expected the caller to be stopped, got: {verdict}"
    assert verdict["stop_signal"] == "SIGTTOU"
    assert "service" not in verdict, f"the caller returned from the call, so it was never frozen: {verdict}"


@pytest.mark.timeout(60)
def test_a_detached_child_cannot_stop_the_caller_when_its_timeout_kills_it(tmp_path: Path) -> None:
    """``is_detached_from_terminal=True``: the child has no terminal to reach, so the kill is contained."""
    verdict = _run_harness(tmp_path, is_detached_from_terminal=True)
    _assert_the_caller_was_backgrounded_on_a_terminal(verdict)

    assert verdict["toucher"]["session_id"] != verdict["session_id"], (
        f"the child was not put in its own session: {verdict}"
    )
    assert verdict["toucher"]["terminal"] == "unreachable", f"the child still reached a terminal: {verdict}"
    assert verdict["toucher"]["errno"] == errno.ENXIO
    assert verdict["outcome"] == "exited", f"expected the caller to run to completion, got: {verdict}"
    assert verdict["exit_code"] == 0

    # The caller not only survived, it got the answer it was owed: the command timed out, and
    # the child died in the SIGTERM handler that would otherwise have taken the group down.
    assert verdict["service"]["is_timed_out"] is True
    assert verdict["service"]["returncode"] == _NO_TERMINAL_EXIT_CODE
