import os
import socket
import threading
import urllib.request

import pytest
from flask import Flask

from imbue.chat.wsgi import INHERITED_SOCKET_ENV
from imbue.chat.wsgi import InheritedSocketMismatchError
from imbue.chat.wsgi import make_threaded_server


def _answer() -> str:
    return "served"


def _application() -> Flask:
    application = Flask(__name__)
    application.add_url_rule("/", view_func=_answer)
    return application


def _listening_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    return listener


def test_the_server_serves_on_the_socket_the_program_line_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = _listening_socket()
    port = listener.getsockname()[1]
    # A connection made before the server exists waits in the backlog rather than being refused.
    early = socket.create_connection(("127.0.0.1", port), timeout=5)
    monkeypatch.setenv(INHERITED_SOCKET_ENV, str(listener.detach()))
    server = make_threaded_server("127.0.0.1", port, _application())
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        assert INHERITED_SOCKET_ENV not in os.environ
        with early:
            early.sendall(b"GET / HTTP/1.0\r\n\r\n")
            assert b"served" in early.recv(4096)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.read() == b"served"
    finally:
        server.shutdown()
        server.server_close()


def test_an_inherited_socket_on_another_port_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = _listening_socket()
    bound_port = listener.getsockname()[1]
    monkeypatch.setenv(INHERITED_SOCKET_ENV, str(listener.detach()))
    with pytest.raises(InheritedSocketMismatchError, match=str(bound_port)):
        make_threaded_server("127.0.0.1", bound_port + 1, _application())
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", bound_port), timeout=5)


def test_without_an_inherited_socket_the_server_binds_its_own() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = make_threaded_server("127.0.0.1", port, _application())
    try:
        assert server.socket.getsockname()[1] == port
    finally:
        server.server_close()
