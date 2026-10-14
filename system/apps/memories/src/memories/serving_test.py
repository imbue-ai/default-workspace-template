import os
import signal
import threading
import urllib.request

import pytest
from flask import Flask

from app_manifest.primitives import AppUrl
from memories.errors import ServeError
from memories.serving import SIGNAL_EXIT_CODE_BASE
from memories.serving import app_url_port
from memories.serving import serve_in_background
from memories.serving import wait_for_shutdown_signal


def test_the_port_is_the_one_the_app_url_names() -> None:
    assert app_url_port(AppUrl("http://localhost:8050")) == 8050
    with pytest.raises(ServeError, match="names no port"):
        app_url_port(AppUrl("http://localhost"))


def test_the_server_answers_inside_the_block_and_a_taken_port_is_refused() -> None:
    app = Flask("served")
    app.add_url_rule("/ping", "ping", lambda: "pong")
    with serve_in_background("127.0.0.1", 0, app) as server:
        port = server.socket.getsockname()[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5) as response:
            assert response.read() == b"pong"
        with pytest.raises(ServeError, match=f"cannot bind 127.0.0.1:{port}"):
            with serve_in_background("127.0.0.1", port, app):
                pass


def test_the_shutdown_wait_returns_128_plus_the_signal_it_got() -> None:
    timer = threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM))
    previous_handler = signal.getsignal(signal.SIGTERM)
    timer.start()
    try:
        assert wait_for_shutdown_signal() == SIGNAL_EXIT_CODE_BASE + signal.SIGTERM
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, previous_handler)
        signal.signal(signal.SIGINT, signal.default_int_handler)


def test_the_shutdown_wait_refuses_to_run_off_the_main_thread() -> None:
    errors: list[ServeError] = []

    def wait_off_main() -> None:
        try:
            wait_for_shutdown_signal()
        except ServeError as e:
            errors.append(e)

    worker = threading.Thread(target=wait_off_main)
    worker.start()
    worker.join(timeout=5)

    assert len(errors) == 1
    assert "main thread" in str(errors[0])
