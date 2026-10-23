from pathlib import Path

import pytest

from skills_catalog.catalog import SkillsCatalog
from skills_catalog.catalog import build_catalog
from skills_catalog.testing import init_workspace_repo


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A checkout whose template commit holds two skills, assist and notify-user."""
    return init_workspace_repo(tmp_path / "workspace", ("assist", "notify-user"))


@pytest.fixture
def catalog(workspace_root: Path) -> SkillsCatalog:
    return build_catalog(workspace_root)
