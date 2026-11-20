import pytest


@pytest.fixture(autouse=True)
def no_registry_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test scans a tree of its own: without this, a workspace that points ``MINDS_APPS_FILE`` at its live
    registry would have the port scan read that registry's ports."""
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)
