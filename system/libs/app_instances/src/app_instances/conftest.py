from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app_instances.blueprint import build_instances_app
from app_instances.data_types import InstanceLifetime
from app_instances.json_store import JsonStoreInstanceSource
from app_instances.primitives import InstanceKeyPrefix, TitleTemplate
from app_instances.testing import (
    RecordedShellRequests,
    RecordingNudger,
    StubInstanceSource,
    serve_recording_shell,
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


@pytest.fixture
def recording_shell() -> Iterator[RecordedShellRequests]:
    with serve_recording_shell() as recorded:
        yield recorded
