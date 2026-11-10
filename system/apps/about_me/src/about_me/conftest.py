from pathlib import Path

import pytest
from flask.testing import FlaskClient

from about_me.server import create_app
from about_me.store import MEMORY_INDEX_PATH
from about_me.store import PROFILE_PATH
from about_me.store import ProfileStore
from about_me.store import SOURCES_PATH
from about_me.testing import SAMPLE_INDEX
from about_me.testing import SAMPLE_PROFILE
from about_me.testing import SAMPLE_SOURCES
from about_me.testing import write_workspace_file


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace root holding the sample profile, memory index and sources."""
    for relative, content in (
        (PROFILE_PATH, SAMPLE_PROFILE),
        (MEMORY_INDEX_PATH, SAMPLE_INDEX),
        (SOURCES_PATH, SAMPLE_SOURCES),
    ):
        write_workspace_file(tmp_path, relative, content)
    return tmp_path


@pytest.fixture
def empty_workspace(tmp_path: Path) -> Path:
    """A workspace root with the memory directory but none of the three files."""
    (tmp_path / MEMORY_INDEX_PATH).parent.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(workspace: Path) -> ProfileStore:
    return ProfileStore(workspace)


@pytest.fixture
def client(workspace: Path) -> FlaskClient:
    return create_app(workspace).test_client()
