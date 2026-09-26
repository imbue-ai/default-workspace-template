import pytest
from flask import Flask

from app_manifest.primitives import AppUrl
from getting_started.errors import ServeError
from getting_started.serving import app_url_port
from getting_started.serving import serve_in_background


def test_the_port_is_the_one_the_app_url_names() -> None:
    assert app_url_port(AppUrl("http://localhost:8030")) == 8030
    with pytest.raises(ServeError, match="names no port"):
        app_url_port(AppUrl("http://localhost"))


def test_a_taken_port_is_refused_with_the_address_named() -> None:
    app = Flask("served")
    with serve_in_background("127.0.0.1", 0, app) as server:
        port = server.socket.getsockname()[1]
        with pytest.raises(ServeError, match=f"cannot bind 127.0.0.1:{port}"):
            with serve_in_background("127.0.0.1", port, app):
                pass
