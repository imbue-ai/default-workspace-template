"""Tests for listen_then_exec.py: bind an app's port before it imports, then exec it on that socket."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workspace_bare_scripts import listen_then_exec

_SCRIPT = Path(listen_then_exec.__file__)

# What the exec'd command does: take the socket the script named, wait (as an importing app would)
# until the release file exists, then answer one connection.
_LATE_SERVER = """
import os, socket, sys, time
listener = socket.socket(fileno=int(os.environ["WERKZEUG_SERVER_FD"]))
while not os.path.exists(sys.argv[1]):
    time.sleep(0.01)
connection, _ = listener.accept()
connection.recv(1024)
connection.sendall(b"HTTP/1.0 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok")
connection.close()
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _connect_when_listening(port: int, deadline: float) -> socket.socket:
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1)
        except ConnectionRefusedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def test_an_address_is_host_colon_port() -> None:
    assert listen_then_exec.parse_address("127.0.0.1:8000") == ("127.0.0.1", 8000)
    for bad in ("8000", "127.0.0.1:", ":8000", "127.0.0.1:http"):
        with pytest.raises(ValueError):
            listen_then_exec.parse_address(bad)


def test_the_listener_is_listening_and_survives_an_exec() -> None:
    listener = listen_then_exec.bind_listener("127.0.0.1", 0)
    try:
        assert listener.get_inheritable()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    finally:
        listener.close()


def test_a_connection_made_before_the_app_serves_is_answered_once_it_does(
    tmp_path: Path,
) -> None:
    port = _free_port()
    release = tmp_path / "release"
    process = subprocess.Popen(
        [
            sys.executable,
            str(_SCRIPT),
            f"127.0.0.1:{port}",
            sys.executable,
            "-c",
            _LATE_SERVER,
            str(release),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with _connect_when_listening(
            port, deadline=time.monotonic() + 10
        ) as connection:
            connection.sendall(b"GET / HTTP/1.0\r\n\r\n")
            connection.settimeout(0.3)
            with pytest.raises(TimeoutError):
                connection.recv(1024)
            release.touch()
            connection.settimeout(10)
            assert connection.recv(1024).startswith(b"HTTP/1.0 200 OK")
        assert process.wait(timeout=10) == 0, (
            process.stderr.read() if process.stderr else ""
        )
    finally:
        process.kill()
        process.wait()


def test_a_port_already_taken_fails_with_a_message() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), f"127.0.0.1:{port}", "true"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 1
    assert f"cannot listen on 127.0.0.1:{port}" in result.stderr


def test_no_command_exits_with_usage() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "127.0.0.1:8000"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
