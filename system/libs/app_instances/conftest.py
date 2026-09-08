from pathlib import Path
from typing import Final

import pytest
from app_instances.testing import SidecarEnvironment, prepare_sidecar_environment

# system/libs/app_instances/conftest.py -> the repository root, the cwd the sidecar resolves
# system/scripts/forward_port.py against.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


@pytest.fixture
def sidecar_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SidecarEnvironment:
    """The cwd, registry, and (unreachable) shell every registration and sidecar under test runs against."""
    return prepare_sidecar_environment(tmp_path, monkeypatch, REPO_ROOT)
