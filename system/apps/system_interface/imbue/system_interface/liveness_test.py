"""Tests for the app liveness probes.

The supervisord probe is exercised against a real XML-RPC server bound to a
unix socket -- the same transport shape supervisord's ``[unix_http_server]``
exposes -- so the custom unix-socket transport is tested end to end rather
than against a faked-out client.
"""

import socket
import socketserver
import threading
import xmlrpc.client
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xmlrpc.server import SimpleXMLRPCDispatcher
from xmlrpc.server import SimpleXMLRPCRequestHandler

import pytest

from imbue.system_interface.liveness import probe_app_liveness
from imbue.system_interface.liveness import probe_supervisor_program
from imbue.system_interface.liveness import probe_tcp_url


class _UnixSocketRequestHandler(SimpleXMLRPCRequestHandler):
    # TCP_NODELAY is meaningless (and an error) on a unix socket.
    disable_nagle_algorithm = False

    def address_string(self) -> str:
        # A unix socket has no peer address; the base implementation indexes
        # into an empty client_address and dies mid-request.
        return "unix-socket"


class _UnixSocketXmlRpcServer(socketserver.ThreadingUnixStreamServer, SimpleXMLRPCDispatcher):
    """A minimal XML-RPC server over a unix socket, standing in for supervisord."""

    # Read by SimpleXMLRPCRequestHandler on every request.
    logRequests = False

    def __init__(self, socket_path: str) -> None:
        SimpleXMLRPCDispatcher.__init__(self, allow_none=False, encoding=None)
        socketserver.ThreadingUnixStreamServer.__init__(self, socket_path, _UnixSocketRequestHandler)


@pytest.fixture
def fake_supervisor_socket(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    """A supervisord-shaped RPC server; the returned dict maps program -> statename."""
    statename_by_program: dict[str, str] = {}

    def get_process_info(name: str) -> dict[str, Any]:
        if name not in statename_by_program:
            raise xmlrpc.client.Fault(10, f"BAD_NAME: {name}")
        return {"name": name, "statename": statename_by_program[name]}

    socket_path = tmp_path / "supervisor.sock"
    server = _UnixSocketXmlRpcServer(str(socket_path))
    server.register_function(get_process_info, "supervisor.getProcessInfo")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path, statename_by_program
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_probe_supervisor_program_reports_a_running_program(
    fake_supervisor_socket: tuple[Path, dict[str, str]],
) -> None:
    socket_path, statename_by_program = fake_supervisor_socket
    statename_by_program["files"] = "RUNNING"
    assert probe_supervisor_program("files", socket_path) is True


def test_probe_supervisor_program_reports_a_starting_program_as_up(
    fake_supervisor_socket: tuple[Path, dict[str, str]],
) -> None:
    socket_path, statename_by_program = fake_supervisor_socket
    statename_by_program["files"] = "STARTING"
    assert probe_supervisor_program("files", socket_path) is True


@pytest.mark.parametrize("statename", ["STOPPED", "STOPPING", "EXITED", "BACKOFF", "FATAL", "UNKNOWN"])
def test_probe_supervisor_program_reports_a_down_program(
    fake_supervisor_socket: tuple[Path, dict[str, str]], statename: str
) -> None:
    socket_path, statename_by_program = fake_supervisor_socket
    statename_by_program["files"] = statename
    assert probe_supervisor_program("files", socket_path) is False


def test_probe_supervisor_program_answers_none_for_an_unknown_program(
    fake_supervisor_socket: tuple[Path, dict[str, str]],
) -> None:
    """A program supervisord does not know (hand-edited registry, removed
    block) is 'cannot say', not 'stopped' -- the caller falls back to TCP."""
    socket_path, _ = fake_supervisor_socket
    assert probe_supervisor_program("no-such-program", socket_path) is None


def test_probe_supervisor_program_answers_none_without_a_socket(tmp_path: Path) -> None:
    assert probe_supervisor_program("files", tmp_path / "absent.sock") is None


@pytest.fixture
def listening_port() -> Iterator[int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


@pytest.fixture
def closed_port() -> int:
    # Bind-then-close: the port existed a moment ago, so nothing else is
    # likely to have claimed it before the probe runs.
    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()
    return port


def test_probe_tcp_url_reports_a_listening_backend(listening_port: int) -> None:
    assert probe_tcp_url(f"http://127.0.0.1:{listening_port}") is True


def test_probe_tcp_url_reports_a_closed_port(closed_port: int) -> None:
    assert probe_tcp_url(f"http://127.0.0.1:{closed_port}") is False


def test_probe_tcp_url_reports_an_unparseable_url_as_down() -> None:
    assert probe_tcp_url("not a url") is False


def test_probe_app_liveness_prefers_the_supervisor_answer(
    fake_supervisor_socket: tuple[Path, dict[str, str]],
    listening_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervised row reads supervisord's state even while something still
    answers on the port (a program mid-STOPPING keeps its socket briefly)."""
    socket_path, statename_by_program = fake_supervisor_socket
    statename_by_program["web"] = "STOPPED"
    monkeypatch.setenv("MINDS_SUPERVISOR_SOCKET", str(socket_path))
    assert probe_app_liveness("web", f"http://127.0.0.1:{listening_port}") is False


def test_probe_app_liveness_falls_back_to_tcp_when_supervisord_cannot_say(
    tmp_path: Path, listening_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_SUPERVISOR_SOCKET", str(tmp_path / "absent.sock"))
    assert probe_app_liveness("web", f"http://127.0.0.1:{listening_port}") is True


def test_probe_app_liveness_probes_tcp_for_an_unsupervised_row(listening_port: int, closed_port: int) -> None:
    assert probe_app_liveness("", f"http://127.0.0.1:{listening_port}") is True
    assert probe_app_liveness("", f"http://127.0.0.1:{closed_port}") is False
