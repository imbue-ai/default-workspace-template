"""Harness for ``test_terminal_isolation.py``; not imported, run as a script in three modes.

Rebuilds the process shape that freezes a workspace's system interface, using only
processes this harness owns:

* ``session`` -- a session leader that owns a pty as its controlling terminal and keeps
  its own process group in the foreground, standing in for the tmux pane running
  ``uv run bootstrap``. It spawns ``service`` into a *new* process group, which is
  therefore a background group on that terminal, exactly as supervisord's ``setpgrp``
  leaves every service it starts. It reports how ``service`` ended -- including the
  signal that stopped it, if one did -- into a JSON file.
* ``service`` -- stands in for the system interface: runs ``toucher`` through
  ``run_local_command_modern_version`` under a timeout it will exceed, so the runner
  kills it with SIGTERM.
* ``toucher`` -- stands in for the ``claude`` CLI: reaches for the controlling terminal
  while handling that SIGTERM.

Run via ``sys.executable`` so the child interpreters match the test's.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import signal
import subprocess
import sys
import termios
from pathlib import Path
from typing import Final

from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version

# The toucher has to boot an interpreter, import this module, and arm its SIGTERM handler
# before this expires -- overrun it and the kill lands on the default disposition, the child
# dies without a marker, and the reproduction quietly stops reproducing. Measured warm at
# 0.08-0.13s, so this is ~40x headroom for a cold, parallel CI runner; the cost of the slack
# is that each harness run waits it out once.
_CHILD_TIMEOUT_SECONDS: Final[float] = 5.0
# The toucher either dies at once or is stopped mid-syscall and never dies, so waiting out
# a full default shutdown budget would only add dead time before the runner's SIGKILL.
_CHILD_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 3.0
# Backstop on every blocking wait in session mode, so a harness that goes wrong fails the
# test with a recorded verdict instead of hanging until pytest's own timeout fires.
_SESSION_DEADLINE_SECONDS: Final[int] = 20
# Exit code for the toucher finding no terminal to reach, distinguishing the detached run
# from any other way the child could have died.
_NO_TERMINAL_EXIT_CODE: Final[int] = 3
# The two signals a terminal uses to stop a background process group.
_JOB_CONTROL_SIGNALS: Final[tuple[int, ...]] = (signal.SIGTTIN, signal.SIGTTOU)


class _DeadlineExpired(Exception):
    """Raised out of the SIGALRM handler to break a blocking wait (see PEP 475)."""


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pin_job_control_signals_to_default() -> str:
    """Take the terminal-stop signals off whatever the test runner left them at.

    The kernel only generates SIGTTIN/SIGTTOU for a process that is neither ignoring nor
    blocking them, so a runner that has turned them off makes the terminal touch succeed
    quietly and the reproduction silently stop reproducing. A supervisord-managed service gets
    the default disposition, which is the one under test, so the harness pins it rather than
    inheriting. The inherited value is returned and recorded in the verdict, which is how a
    platform that does not deliver the stop for some *other* reason can be told apart from one
    that merely had the signals switched off.
    """
    inherited = ",".join(str(signal.getsignal(number)) for number in _JOB_CONTROL_SIGNALS)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, set(_JOB_CONTROL_SIGNALS))
    for number in _JOB_CONTROL_SIGNALS:
        signal.signal(number, signal.SIG_DFL)
    return inherited


def _raise_deadline_expired(signal_number: int, frame: object) -> None:
    raise _DeadlineExpired()


def _reach_for_the_terminal_and_exit(signal_number: int, frame: object) -> None:
    """SIGTERM handler for toucher mode. Reads its marker path back out of ``sys.argv``."""
    marker_path = Path(sys.argv[2])
    inherited = _pin_job_control_signals_to_default()
    try:
        terminal_fd = os.open("/dev/tty", os.O_RDWR)
    except OSError as e:
        _write_json(
            marker_path,
            {
                "terminal": "unreachable",
                "errno": e.errno,
                "session_id": os.getsid(0),
                "inherited_job_control_signals": inherited,
            },
        )
        os._exit(_NO_TERMINAL_EXIT_CODE)
    # Recorded before the call, not after: when the terminal is reachable from a background
    # process group this process is stopped *inside* tcsetattr and never runs again, so
    # "reached" is the last thing it can report.
    _write_json(
        marker_path,
        {"terminal": "reached", "session_id": os.getsid(0), "inherited_job_control_signals": inherited},
    )
    termios.tcsetattr(terminal_fd, termios.TCSADRAIN, termios.tcgetattr(terminal_fd))
    _write_json(
        marker_path,
        {
            "terminal": "reached_and_returned",
            "session_id": os.getsid(0),
            "inherited_job_control_signals": inherited,
        },
    )
    os._exit(0)


def _run_toucher(marker_path: Path) -> int:
    signal.signal(signal.SIGTERM, _reach_for_the_terminal_and_exit)
    _write_json(marker_path, {"terminal": "waiting", "session_id": os.getsid(0)})
    # This process exists only to be signalled, and the handler exits without returning.
    signal.pause()
    return 0


def _run_service(result_path: Path, marker_path: Path, is_detached_from_terminal: bool) -> int:
    # The caller has to be stoppable too: SIGTTIN/SIGTTOU go to the whole process group, and a
    # caller still ignoring them would survive a kill that the real service does not.
    inherited = _pin_job_control_signals_to_default()
    finished = run_local_command_modern_version(
        [sys.executable, __file__, "toucher", str(marker_path)],
        is_checked=False,
        timeout=_CHILD_TIMEOUT_SECONDS,
        shutdown_timeout_sec=_CHILD_SHUTDOWN_TIMEOUT_SECONDS,
        is_detached_from_terminal=is_detached_from_terminal,
    )
    _write_json(
        result_path,
        {
            "is_timed_out": finished.is_timed_out,
            "returncode": finished.returncode,
            "session_id": os.getsid(0),
            "process_group_id": os.getpgid(0),
            "inherited_job_control_signals": inherited,
        },
    )
    return 0


def _wait_under_deadline(pid: int, options: int) -> int | None:
    """``waitpid`` bounded by SIGALRM; ``None`` means the deadline expired first."""
    signal.alarm(_SESSION_DEADLINE_SECONDS)
    try:
        return os.waitpid(pid, options)[1]
    except _DeadlineExpired:
        return None
    finally:
        signal.alarm(0)


def _run_session(result_path: Path, marker_path: Path, is_detached_from_terminal: bool) -> int:
    signal.signal(signal.SIGALRM, _raise_deadline_expired)

    service_result_path = result_path.with_suffix(".service.json")
    _master_fd, terminal_fd = pty.openpty()
    os.setsid()
    fcntl.ioctl(terminal_fd, termios.TIOCSCTTY, 0)

    service = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "service",
            str(service_result_path),
            str(marker_path),
            "detached" if is_detached_from_terminal else "attached",
        ],
        stdin=subprocess.DEVNULL,
        # A new process group, mirroring supervisord's setpgrp: same session and same
        # controlling terminal as us, but not the terminal's foreground group.
        process_group=0,
    )
    verdict: dict[str, object] = {
        "session_id": os.getsid(0),
        "foreground_process_group_id": os.tcgetpgrp(terminal_fd),
        "own_process_group_id": os.getpgid(0),
        "service_pid": service.pid,
        "service_process_group_id": os.getpgid(service.pid),
        "service_session_id": os.getsid(service.pid),
    }
    # Published before the first blocking wait so the test can still clean up the service
    # group if this process is killed while the service sits stopped.
    _write_json(result_path, {**verdict, "outcome": "running"})

    status = _wait_under_deadline(service.pid, os.WUNTRACED)
    if status is None:
        verdict["outcome"] = "deadline_expired"
    elif os.WIFSTOPPED(status):
        verdict["outcome"] = "stopped"
        verdict["stop_signal"] = signal.Signals(os.WSTOPSIG(status)).name
    else:
        verdict["outcome"] = "exited"
        verdict["exit_code"] = os.WEXITSTATUS(status)

    if verdict["outcome"] != "exited":
        # Kill the whole group rather than resuming it: SIGCONT alone does not stick,
        # because the resumed toucher re-enters tcsetattr and instantly re-stops the group.
        try:
            os.killpg(service.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if _wait_under_deadline(service.pid, 0) is None:
            verdict["outcome"] = "service_unreapable"
    service.poll()

    if service_result_path.exists():
        verdict["service"] = json.loads(service_result_path.read_text(encoding="utf-8"))
    _write_json(result_path, verdict)
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1]
    if mode == "toucher":
        return _run_toucher(Path(argv[2]))
    is_detached_from_terminal = argv[4] == "detached"
    if mode == "service":
        return _run_service(Path(argv[2]), Path(argv[3]), is_detached_from_terminal)
    assert mode == "session", f"unknown mode {mode!r}"
    return _run_session(Path(argv[2]), Path(argv[3]), is_detached_from_terminal)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
