from collections.abc import Iterator
from pathlib import Path

import pytest
from app_instances.blueprint import build_instances_app
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import RecordingNudger
from app_instances.testing import StubInstanceSource
from app_instances.testing import free_port
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.shell.testing import FakeInstanceFetcher
from imbue.system_interface.shell.testing import TEST_TERMINAL_URL
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


@pytest.fixture
def stub_source() -> StubInstanceSource:
    """The in-memory instance source behind ``stub_app_url``; tests seed and inspect its records."""
    return StubInstanceSource()


@pytest.fixture
def stub_app_url(stub_source: StubInstanceSource) -> Iterator[str]:
    """A real instances API served over loopback for the block, so the relay and the fetcher run against the wire."""
    port = free_port()
    with serve_in_background(LOOPBACK_HOST, port, build_instances_app(stub_source, RecordingNudger())):
        yield f"http://{LOOPBACK_HOST}:{port}"


@pytest.fixture
def fetcher() -> FakeInstanceFetcher:
    """The two-app registry's fetcher: the terminal lists one instance."""
    fetcher = FakeInstanceFetcher()
    fetcher.list(TEST_TERMINAL_URL, instance_record("terminal-1", "Terminal 1"))
    return fetcher


@pytest.fixture
def app(tmp_path: Path, broadcaster: WebSocketBroadcaster, fetcher: FakeInstanceFetcher) -> Flask:
    """The shell app over the two-app registry, with the terminal's list already fetched."""
    inventory = build_inventory(write_two_app_registry(tmp_path), broadcaster, fetcher=fetcher)
    inventory.refetch_now("terminal")
    return shell_application(tmp_path, inventory, broadcaster)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()
