"""Serving the page: the port the app URL names, a background server with HTTP/1.1 (the WebSockets need it), and
the wait for supervisord's SIGTERM."""

import signal
import socket
import threading
import urllib.parse
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import Final

from flask import Flask
from loguru import logger
from werkzeug.serving import BaseWSGIServer
from werkzeug.serving import LISTEN_QUEUE
from werkzeug.serving import WSGIRequestHandler
from werkzeug.serving import make_server

from app_manifest.primitives import AppUrl
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from screen.errors import ServeError

SERVER_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0
SIGNAL_EXIT_CODE_BASE: Final[int] = 128
_STOP_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)


class Http11RequestHandler(WSGIRequestHandler):
    """Werkzeug's handler pinned to HTTP/1.1: keepalive and the incremental flush the proxied streams need."""

    protocol_version = "HTTP/1.1"


@pure
def app_url_port(app_url: AppUrl) -> int:
    try:
        port = urllib.parse.urlsplit(app_url).port
    except ValueError as e:
        raise ServeError(f"the app URL {app_url!r} names no usable port: {e}") from e
    if port is None:
        raise ServeError(f"the app URL {app_url!r} names no port")
    return port


def _bind_listening_socket(host: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError as e:
        listener.close()
        raise ServeError(f"cannot bind {host}:{port}: {e}") from e
    listener.listen(LISTEN_QUEUE)
    return listener


@contextmanager
def serve_in_background(host: str, port: int, app: Flask) -> Generator[BaseWSGIServer, None, None]:
    """Serve ``app`` at ``host:port`` on a daemon thread for the block."""
    with log_span("Starting a server at {}:{}", host, port):
        with _bind_listening_socket(host, port) as listener:
            server = make_server(
                host, port, app, threaded=True, fd=listener.fileno(), request_handler=Http11RequestHandler
            )
        server_thread = threading.Thread(target=server.serve_forever, name=f"serve-{host}:{port}", daemon=True)
        server_thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        if server_thread.is_alive():
            logger.warning("Stopped the server at {}:{} but its thread is still running", host, port)


def wait_for_shutdown_signal() -> int:
    """Block until SIGTERM or SIGINT arrives, and return the exit status for it (128 plus the signal number)."""
    if threading.current_thread() is not threading.main_thread():
        raise ServeError("the shutdown wait must run on the main thread so it can install signal handlers")
    received: list[int] = []
    stop = threading.Event()

    def note(signum: int, _frame: FrameType | None) -> None:
        received.append(signum)
        stop.set()

    for stop_signal in _STOP_SIGNALS:
        signal.signal(stop_signal, note)
    stop.wait()
    return SIGNAL_EXIT_CODE_BASE + received[0]
