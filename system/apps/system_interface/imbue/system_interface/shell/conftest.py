from collections.abc import Iterator

import pytest
from app_instances.blueprint import build_instances_app
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import RecordingNudger
from app_instances.testing import StubInstanceSource
from app_instances.testing import free_port


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
