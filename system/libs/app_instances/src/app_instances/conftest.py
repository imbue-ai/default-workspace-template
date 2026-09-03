from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask, request
from flask.testing import FlaskClient

from app_instances.blueprint import build_instances_app
from app_instances.data_types import InstanceLifetime
from app_instances.json_store import JsonStoreInstanceSource
from app_instances.primitives import InstanceKeyPrefix, TitleTemplate
from app_instances.sidecar import serve_in_background
from app_instances.testing import (
    LOOPBACK_HOST,
    RecordingNudger,
    StubInstanceSource,
    free_port,
)


@pytest.fixture
def stub_source() -> StubInstanceSource:
    return StubInstanceSource()


@pytest.fixture
def recording_nudger() -> RecordingNudger:
    return RecordingNudger()


@pytest.fixture
def instances_client(
    stub_source: StubInstanceSource, recording_nudger: RecordingNudger
) -> FlaskClient:
    return build_instances_app(stub_source, recording_nudger).test_client()


@pytest.fixture
def files_store(tmp_path: Path) -> JsonStoreInstanceSource:
    """A store wired the way the files app will wire it: referenced, not renameable, location tracked."""
    return JsonStoreInstanceSource(
        store_path=tmp_path / "instances.json",
        key_prefix=InstanceKeyPrefix("files"),
        title_template=TitleTemplate("File Viewer {n}"),
        lifetime=InstanceLifetime.REFERENCED,
        is_renameable=False,
        is_location_tracked=True,
    )


@pytest.fixture
def renameable_store(tmp_path: Path) -> JsonStoreInstanceSource:
    return JsonStoreInstanceSource(
        store_path=tmp_path / "renameable" / "instances.json",
        key_prefix=InstanceKeyPrefix("note"),
        title_template=TitleTemplate("Note {n}"),
        lifetime=InstanceLifetime.EXPLICIT,
        is_renameable=True,
        is_location_tracked=False,
    )


class RecordedShellRequests:
    """What a fake shell received, and where it listens."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.requests: list[tuple[str, str]] = []


@pytest.fixture
def recording_shell() -> Iterator[RecordedShellRequests]:
    """A loopback server that records every (method, path) and answers 404, as the shell does before phase 7."""
    port = free_port()
    recorded = RecordedShellRequests(base_url=f"http://{LOOPBACK_HOST}:{port}")
    app = Flask(__name__)

    @app.route("/<path:_anything>", methods=["GET", "POST"])
    def record(_anything: str) -> tuple[str, int]:
        recorded.requests.append((request.method, request.path))
        return "", 404

    with serve_in_background(LOOPBACK_HOST, port, app):
        yield recorded
