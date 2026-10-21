from pathlib import Path

import pytest
from flask.testing import FlaskClient

from skills_app.server import STATIC_DIR
from skills_app.server import create_app
from skills_catalog.catalog import build_catalog
from skills_catalog.testing import init_workspace_repo


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A checkout whose template commit holds two skills, assist and notify-user."""
    return init_workspace_repo(tmp_path / "workspace", ("assist", "notify-user"))


@pytest.fixture
def client(workspace_root: Path) -> FlaskClient:
    return create_app(build_catalog(workspace_root), STATIC_DIR).test_client()
