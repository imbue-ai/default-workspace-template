from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from mock_messenger_test import RecordingLauncher
from mock_messenger_test import RecordingMessenger
from mock_neko_test import FakeNeko
from mock_supervisor_test import FakeSupervisor
from mock_window_tools_test import FakeWindowTools
from screen.proxy import NekoProxy
from screen.routes import build_app
from screen.service import ScreenService
from screen.stack import ScreenStack


@pytest.fixture
def fake_supervisor(tmp_path: Path) -> FakeSupervisor:
    return FakeSupervisor(display_socket_path=tmp_path / "x11" / "X99")


@pytest.fixture
def fake_neko() -> FakeNeko:
    return FakeNeko()


@pytest.fixture
def recording_messenger() -> RecordingMessenger:
    return RecordingMessenger()


@pytest.fixture
def service(
    tmp_path: Path, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko, recording_messenger: RecordingMessenger
) -> Iterator[ScreenService]:
    """A service over fakes whose stack starts at once, with no event subscription."""
    built = ScreenService(
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        neko=fake_neko,
        window_tools=FakeWindowTools(),
        launcher=RecordingLauncher(),
        messenger=recording_messenger,
        is_event_subscription_enabled=False,
    )
    stack = ScreenStack(
        supervisor=fake_supervisor,
        neko=fake_neko,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
        install_check=lambda: None,
        neko_health_timeout_seconds=1.0,
        install_poll_seconds=0.01,
        on_lifecycle_changed=built.on_lifecycle_changed,
        on_running=built.on_running,
        set_api_token=built.set_api_token,
    )
    built.attach_stack(stack)
    built.start()
    yield built
    built.shutdown("the test ended")


@pytest.fixture
def app(service: ScreenService, tmp_path: Path) -> Flask:
    return build_app(service, tmp_path / "static", NekoProxy())


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()
