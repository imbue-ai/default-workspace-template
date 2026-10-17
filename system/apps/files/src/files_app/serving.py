"""Serving: the Files server on a background thread, the wait for dufs to come up, and the wait for supervisord's
SIGTERM or for dufs to exit. The server and signal handling follow the Getting Started app's."""

import signal
import socket
import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import Final

from flask import Flask
from loguru import logger
from werkzeug.serving import BaseWSGIServer
from werkzeug.serving import LISTEN_QUEUE
from werkzeug.serving import make_server

from imbue.imbue_common.logging import log_span

SERVER_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0
SIGNAL_EXIT_CODE_BASE: Final[int] = 128
PROCESS_EXITED_STATUS: Final[int] = 1
_STOP_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)
_POLL_SECONDS: Final[float] = 0.1


class ServeError(Exception):
    """The server could not start."""


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
    with log_span("Starting the Files server at {}:{}", host, port):
        # Bound here rather than by make_server, which answers a bind failure with sys.exit(1), not an exception.
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
            logger.warning("Stopped the Files server but its thread is still running")


def wait_for_port(host: str, port: int, timeout_seconds: float, process: subprocess.Popen[bytes]) -> None:
    """Return once something accepts connections at ``host:port``; raise if ``process`` exits or time runs out."""
    pause = threading.Event()
    waited = 0.0
    while waited < timeout_seconds:
        if process.poll() is not None:
            raise ServeError(f"dufs exited with status {process.returncode} before it was serving")
        try:
            with socket.create_connection((host, port), timeout=_POLL_SECONDS):
                return
        except OSError:
            pause.wait(_POLL_SECONDS)
            waited += _POLL_SECONDS
    raise ServeError(f"dufs did not start serving on {host}:{port} within {timeout_seconds}s")


def wait_for_shutdown_signal(process: subprocess.Popen[bytes]) -> int:
    """Block until SIGTERM or SIGINT arrives, and return the exit status for it (128 plus the signal number); or
    until ``process`` exits, returning 1, so supervisord restarts the app rather than leave it serving without dufs."""
    received: list[int] = []
    stop = threading.Event()

    def note(signum: int, _frame: FrameType | None) -> None:
        received.append(signum)
        stop.set()

    for stop_signal in _STOP_SIGNALS:
        signal.signal(stop_signal, note)
    while not stop.wait(_POLL_SECONDS):
        if process.poll() is not None:
            logger.error("dufs exited with status {}; stopping Files so supervisord restarts it", process.returncode)
            return PROCESS_EXITED_STATUS
    return SIGNAL_EXIT_CODE_BASE + received[0]
