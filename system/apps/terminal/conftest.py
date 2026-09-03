from pathlib import Path
from typing import Final

import pytest
from app_instances.nudge import ENV_SHELL_URL
from app_instances.testing import LOOPBACK_HOST, SidecarEnvironment, free_port
from app_manifest.registry import ENV_APPS_FILE

# system/apps/terminal/conftest.py -> the repository root, the cwd the app resolves
# system/scripts/forward_port.py against.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


@pytest.fixture
def terminal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SidecarEnvironment:
    """The cwd, registry, and (unreachable) shell the terminal app under test runs against."""
    monkeypatch.chdir(REPO_ROOT)
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry_path))
    monkeypatch.setenv(ENV_SHELL_URL, f"http://{LOOPBACK_HOST}:{free_port()}")
    return SidecarEnvironment(scratch_dir=tmp_path, registry_path=registry_path)
