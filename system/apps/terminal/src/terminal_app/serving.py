"""Serving the wrapper pages: the port an app URL names, a background server, and the wait for supervisord's SIGTERM."""

import signal
import socket
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Final

from app_manifest.primitives import AppUrl
from flask import Flask
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from loguru import logger
from werkzeug.serving import LISTEN_QUEUE, BaseWSGIServer, make_server

from terminal_app.errors import TerminalServeError

SERVER_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0

# The shell convention for a process killed by a signal: 128 plus the signal number.
SIGNAL_EXIT_CODE_BASE: Final[int] = 128

_STOP_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)


@pure
def app_url_port(app_url: AppUrl) -> int:
    """The port a server must listen on: the one the app URL names."""
    try:
        port = urllib.parse.urlsplit(app_url).port
    except ValueError as e:
        raise TerminalServeError(f"the app URL {app_url!r} names no usable port: {e}") from e
    if port is None:
        raise TerminalServeError(f"the app URL {app_url!r} names no port")
    return port


def _bind_listening_socket(host: str, port: int) -> socket.socket:
    """A bound, listening TCP socket at ``host:port``; raises TerminalServeError when the address cannot be bound."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError as e:
        listener.close()
        raise TerminalServeError(f"cannot bind {host}:{port}: {e}") from e
    listener.listen(LISTEN_QUEUE)
    return listener


@contextmanager
def serve_in_background(host: str, port: int, app: Flask) -> Iterator[BaseWSGIServer]:
    """Serve ``app`` at ``host:port`` on a daemon thread for the block; the socket accepts connections once the
    block is entered and is closed when it ends."""
    with log_span("Starting a server at {}:{}", host, port):
        # The socket is bound here rather than by make_server: werkzeug answers a bind failure
        # with a message on stderr and sys.exit(1), never an exception. Given a descriptor it
        # duplicates it and skips its own bind, so this handle can close once the server has its own.
        with _bind_listening_socket(host, port) as listener:
            server = make_server(host, port, app, threaded=True, fd=listener.fileno())
        server_thread = threading.Thread(target=server.serve_forever, name=f"serve-{host}:{port}", daemon=True)
        server_thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        if server_thread.is_alive():
            logger.warning(
                "Stopped the server at {}:{} but its thread is still running {}s later",
                host,
                port,
                SERVER_SHUTDOWN_TIMEOUT_SECONDS,
            )


def wait_for_shutdown_signal() -> int:
    """Block until SIGTERM or SIGINT arrives, and return the exit status for it (128 plus the signal number).

    What the app runs on its main thread once its server is listening: supervisord stops it with
    SIGTERM, and the status says so.
    """
    if threading.current_thread() is not threading.main_thread():
        raise TerminalServeError("the shutdown wait must run on the main thread so it can install signal handlers")
    received: list[int] = []
    stop = threading.Event()

    def note(signum: int, _frame: FrameType | None) -> None:
        received.append(signum)
        stop.set()

    for stop_signal in _STOP_SIGNALS:
        signal.signal(stop_signal, note)
    stop.wait()
    return SIGNAL_EXIT_CODE_BASE + received[0]
