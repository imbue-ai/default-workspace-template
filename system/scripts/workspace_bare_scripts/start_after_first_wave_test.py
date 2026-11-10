"""Tests for start_after_first_wave.py: the boot gate that holds the second wave until the shell and chat serve."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from workspace_bare_scripts import start_after_first_wave as gate

_SCRIPT = Path(gate.__file__)


class _FakeClock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _serving_after(clock: _FakeClock, at: float) -> Callable[[], bool]:
    return lambda: clock.now >= at


def test_a_program_started_inside_the_window_once_the_first_wave_serves_waits_only_its_delay() -> (
    None
):
    clock = _FakeClock(now=1_000.0)
    hold = gate.seconds_to_hold(
        delay_seconds=5.0,
        started_at=995.0,
        is_serving=lambda: True,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert hold == 5.0
    assert clock.sleeps == []


def test_a_program_started_after_the_window_is_not_held_even_with_the_first_wave_down() -> (
    None
):
    clock = _FakeClock(now=1_000.0)
    hold = gate.seconds_to_hold(
        delay_seconds=5.0,
        started_at=1_000.0 - gate.FIRST_WAVE_WINDOW_SECONDS - 1,
        is_serving=lambda: False,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert hold == 0.0
    assert clock.sleeps == []


def test_a_held_program_waits_for_the_first_wave_then_its_delay() -> None:
    clock = _FakeClock(now=1_000.0)
    hold = gate.seconds_to_hold(
        delay_seconds=6.0,
        started_at=1_000.0,
        is_serving=_serving_after(clock, 1_003.0),
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert hold == 6.0
    assert clock.now == 1_003.0
    assert set(clock.sleeps) == {gate.POLL_SECONDS}


def test_a_first_wave_that_never_serves_holds_nobody_past_the_window() -> None:
    clock = _FakeClock(now=1_000.0)
    hold = gate.seconds_to_hold(
        delay_seconds=2.0,
        started_at=1_000.0,
        is_serving=lambda: False,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert hold == 2.0
    assert clock.now == 1_000.0 + gate.FIRST_WAVE_WINDOW_SECONDS


def test_with_no_supervisord_the_window_runs_from_now() -> None:
    clock = _FakeClock(now=1_000.0)
    gate.seconds_to_hold(
        delay_seconds=0.0,
        started_at=None,
        is_serving=lambda: False,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert clock.now == 1_000.0 + gate.FIRST_WAVE_WINDOW_SECONDS


def test_supervisords_start_is_its_pid_files_mtime(tmp_path: Path) -> None:
    pidfile = tmp_path / "supervisord.pid"
    pidfile.write_text("1\n")
    os.utime(pidfile, (1_234.0, 1_234.0))
    assert gate.supervisord_started_at(pidfile) == 1_234.0
    assert gate.supervisord_started_at(tmp_path / "absent.pid") is None


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(503)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _http_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_any_http_answer_counts_as_serving() -> None:
    with _http_server() as port:
        assert gate.is_answering(port, "/api/health")


def test_a_closed_port_is_not_serving() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert not gate.is_answering(port, "/api/health")


def test_a_bound_port_whose_app_is_still_importing_is_not_serving() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        started = time.monotonic()
        assert not gate.is_answering(listener.getsockname()[1], "/api/health")
        assert time.monotonic() - started < 3


def test_the_command_is_execed_with_its_arguments_untouched(tmp_path: Path) -> None:
    pidfile = tmp_path / "supervisord.pid"
    pidfile.write_text("1\n")
    long_ago = time.time() - 3_600
    os.utime(pidfile, (long_ago, long_ago))
    recorded = tmp_path / "recorded.txt"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--delay",
            "60",
            "--supervisord-pidfile",
            str(pidfile),
            "bash",
            "-c",
            f'printf "%s|" "$0" "$@" > {recorded}',
            "first",
            "--flag",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert recorded.read_text() == "first|--flag|"


def test_no_command_exits_with_usage() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--delay", "1"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
