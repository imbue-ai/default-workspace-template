#!/usr/bin/env python3
"""Hold a supervisord program back at boot until the shell and the chat app are serving, then exec it.

supervisord starts every ``autostart`` program in the same instant. On a small machine that puts the
two programs a new user waits on -- the shell, which paints the desktop, and the chat app, which owns
the AI-provider chooser and its sign-in -- behind every other program's cold start. A program whose
command is prefixed with this script (``python3 <this script> --delay N <command...>``, after the
memory band wrapper) is part of the *second wave*: at boot it waits until both apps answer their
``/api/health``, then a further ``--delay`` seconds so the second wave's heavy first-boot work
(the ``uv sync`` runs, env-converge's slow phase, the backups) does not all start in one instant,
and only then execs its command with its arguments untouched.

The wait is bounded and boot-scoped:

- It never outlasts ``FIRST_WAVE_WINDOW_SECONDS`` after supervisord started (the mtime of its pid
  file), so a shell or chat that cannot start holds nothing back for longer than that.
- A program started once both apps already answer -- a restart, a wake from the shell's port
  parking, an update's ``supervisorctl update`` -- execs at once, with no ``--delay``: the stagger
  only spreads out programs that were actually held back.
- A program started after that window (supervisord has been up for longer) execs at once.

Every program keeps ``autostart=true``, so a restart of the services agent still starts them all.
Stdlib only: supervisord runs it under the system ``python3``.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

# The first wave's health routes: the shell on the port its program line registers
# (system/supervisord.conf.d/system_interface.conf), the chat app on its default port
# (imbue.chat.config.DEFAULT_CHAT_PORT). system/test_supervisord_layout.py pins both.
SHELL_HEALTH: tuple[int, str] = (8000, "/api/health")
CHAT_HEALTH: tuple[int, str] = (8010, "/api/health")
FIRST_WAVE: tuple[tuple[int, str], ...] = (SHELL_HEALTH, CHAT_HEALTH)

# Where supervisord.conf's ``pidfile`` puts supervisord's pid; its mtime is when supervisord started.
SUPERVISORD_PIDFILE = Path("/var/run/supervisord.pid")

# How long after supervisord starts the second wave may be held back for the first.
FIRST_WAVE_WINDOW_SECONDS = 30.0
POLL_SECONDS = 0.25
_CONNECT_TIMEOUT_SECONDS = 0.25
# The shell and the chat bind their port before they import (app_registration.listener), so a
# connect succeeds early and the answer arrives once the app serves.
_ANSWER_TIMEOUT_SECONDS = 1.0


def is_answering(port: int, path: str) -> bool:
    """Whether an HTTP server on loopback ``port`` answers ``path`` with any status."""
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=_CONNECT_TIMEOUT_SECONDS
        ) as connection:
            connection.settimeout(_ANSWER_TIMEOUT_SECONDS)
            connection.sendall(
                f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode("ascii")
            )
            return connection.recv(5) == b"HTTP/"
    except OSError:
        return False


def is_first_wave_serving() -> bool:
    return all(is_answering(port, path) for port, path in FIRST_WAVE)


def supervisord_started_at(pidfile: Path) -> float | None:
    """When supervisord started (its pid file's mtime), or None with no pid file (a run outside supervisord)."""
    try:
        return pidfile.stat().st_mtime
    except OSError:
        return None


def seconds_to_hold(
    delay_seconds: float,
    started_at: float | None,
    is_serving: Callable[[], bool],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> float:
    """Wait for the first wave as the module docstring says; how long to sleep before the exec.

    ``started_at`` None (no supervisord) measures the window from now.
    """
    now = clock()
    window_end = (now if started_at is None else started_at) + FIRST_WAVE_WINDOW_SECONDS
    if now >= window_end or is_serving():
        return 0.0
    while clock() < window_end and not is_serving():
        sleep(POLL_SECONDS)
    return delay_seconds


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start a second-wave program once the shell and the chat app serve"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait after the first wave serves, when this program was held back for it",
    )
    parser.add_argument(
        "--supervisord-pidfile",
        type=Path,
        default=SUPERVISORD_PIDFILE,
        help="The file whose mtime is when supervisord started",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="The program's own command"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not args.command:
        print(
            "usage: start_after_first_wave.py [--delay SECONDS] <command> [args...]",
            file=sys.stderr,
        )
        sys.exit(2)
    hold = seconds_to_hold(
        delay_seconds=args.delay,
        started_at=supervisord_started_at(args.supervisord_pidfile),
        is_serving=is_first_wave_serving,
        clock=time.time,
        sleep=time.sleep,
    )
    if hold > 0:
        time.sleep(hold)
    os.execvp(args.command[0], args.command)


if __name__ == "__main__":
    main()
