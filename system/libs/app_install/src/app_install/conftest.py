import pytest


@pytest.fixture(autouse=True)
def no_registry_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test scans a tree of its own: without this, a workspace that points ``MINDS_APPS_FILE`` at its live
    registry would have the port scan read that registry's ports."""
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)


@pytest.fixture(autouse=True)
def no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads a repository through the gateway of the workspace it happens to run in."""
    for name in ("LATCHKEY_GATEWAY", "LATCHKEY_GATEWAY_PASSWORD", "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)
