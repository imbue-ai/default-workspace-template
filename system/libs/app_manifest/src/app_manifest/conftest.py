from pathlib import Path

import pytest

from app_manifest.registry_location import ENV_APPS_FILE


@pytest.fixture
def registration_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch registry for ``register_app``: ``ENV_APPS_FILE`` pointing at it; returns its path."""
    registry = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry))
    return registry
