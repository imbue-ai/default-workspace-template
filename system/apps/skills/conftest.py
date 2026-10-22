import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from skills_app.server import STATIC_DIR
from skills_app.server import create_app
from skills_app.testing import FakeSkillIndexSearcher
from skills_app.testing import LocalSkillIndex
from skills_catalog.catalog import build_catalog
from skills_catalog.testing import init_workspace_repo


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A checkout whose template commit holds two skills, assist and notify-user."""
    return init_workspace_repo(tmp_path / "workspace", ("assist", "notify-user"))


@pytest.fixture
def searcher() -> FakeSkillIndexSearcher:
    """An online index that answers nothing until a test gives it matches."""
    return FakeSkillIndexSearcher()


@pytest.fixture
def client(workspace_root: Path, searcher: FakeSkillIndexSearcher) -> FlaskClient:
    return create_app(build_catalog(workspace_root), STATIC_DIR, searcher).test_client()


@pytest.fixture
def local_skill_index() -> Iterator[LocalSkillIndex]:
    index = LocalSkillIndex()
    thread = threading.Thread(target=index.server.serve_forever, daemon=True)
    thread.start()
    yield index
    index.server.shutdown()
    index.server.server_close()
