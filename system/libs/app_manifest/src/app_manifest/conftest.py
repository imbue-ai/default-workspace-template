from collections.abc import Iterator
from pathlib import Path

import pytest

from app_manifest.registry import ENV_APPS_FILE
from app_manifest.testing import ShellStub

# system/libs/app_manifest/src/app_manifest/conftest.py -> the repository root, the cwd the
# registration script is resolved against.
_REPO_ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def registration_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch registry for ``register_app``: cwd at the repo root (the registration script is
    cwd-relative) and ``ENV_APPS_FILE`` pointing at the registry; returns the registry path."""
    monkeypatch.chdir(_REPO_ROOT)
    registry = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry))
    return registry


@pytest.fixture
def shell_stub() -> Iterator[ShellStub]:
    stub = ShellStub()
    stub.start()
    try:
        yield stub
    finally:
        stub.close()
