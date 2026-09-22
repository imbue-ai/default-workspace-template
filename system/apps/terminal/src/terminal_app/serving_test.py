import os
import signal
import threading

import pytest
from app_manifest.primitives import AppUrl
from flask import Flask
from imbue.mngr.utils.polling import wait_for

from terminal_app.errors import TerminalServeError
from terminal_app.serving import (
    SIGNAL_EXIT_CODE_BASE,
    app_url_port,
    serve_in_background,
    wait_for_shutdown_signal,
)
from terminal_app.testing import LOOPBACK_HOST, free_port, is_port_accepting


def test_app_url_port_reads_the_port_the_app_url_names() -> None:
    assert app_url_port(AppUrl("http://localhost:8080")) == 8080
    with pytest.raises(TerminalServeError, match="names no port"):
        app_url_port(AppUrl("http://localhost"))
    with pytest.raises(TerminalServeError, match="names no usable port"):
        app_url_port(AppUrl("http://localhost:seven"))


def test_serve_in_background_accepts_while_entered_and_releases_the_port_on_exit() -> (
    None
):
    port = free_port()
    app = Flask("serving-test")

    with serve_in_background(LOOPBACK_HOST, port, app):
        assert is_port_accepting(port)
        with pytest.raises(TerminalServeError, match="cannot bind"):
            with serve_in_background(LOOPBACK_HOST, port, app):
                pass

    assert not is_port_accepting(port)


def test_wait_for_shutdown_signal_refuses_to_run_off_the_main_thread() -> None:
    raised: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            wait_for_shutdown_signal()
        except TerminalServeError as e:
            raised.append(e)

    worker = threading.Thread(target=run_in_thread)
    worker.start()
    worker.join(timeout=5)

    assert len(raised) == 1
    assert "main thread" in str(raised[0])


def test_wait_for_shutdown_signal_returns_128_plus_the_signal() -> None:
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }

    def send_sigterm_once_the_wait_listens() -> None:
        # Only a handler the wait installed may receive the signal: the default disposition
        # would end the test process.
        wait_for(
            lambda: signal.getsignal(signal.SIGTERM)
            is not previous_handlers[signal.SIGTERM],
            timeout=5.0,
            poll_interval=0.02,
            error_message="the shutdown wait never installed its handler",
        )
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_sigterm_once_the_wait_listens)
    try:
        sender.start()
        assert wait_for_shutdown_signal() == SIGNAL_EXIT_CODE_BASE + signal.SIGTERM
    finally:
        sender.join(timeout=5)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
