from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


@pytest.fixture
def app(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> Flask:
    """The shell app over the two-app registry."""
    inventory = build_inventory(write_two_app_registry(tmp_path), broadcaster)
    return shell_application(tmp_path, inventory, broadcaster)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()
