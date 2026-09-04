"""The instances API as the daemon serves it: the blueprint mounted on the real Flask app,
over the real manager and bridge (started once by the conftest), with fake in-memory
browsers standing in for Chromium."""

from pathlib import Path
from typing import Any

import pytest
from app_instances.testing import RecordingNudger
from app_manifest.manifest import load_manifest
from browser import runner
from browser import session as bsession

# The manifest the supervisord program line registers with ``forward_port.py --manifest``.
_APP_MANIFEST_PATH = Path(__file__).parent / "app.toml"


def _install_running_browser(name: str) -> bsession.LiveBrowser:
    """A browser the manager holds as ``running``, with no Chromium behind it."""
    fake = bsession.LiveBrowser(browser_id=name)
    fake._lifecycle = "running"
    runner.manager._browsers[name] = fake
    return fake


def _instances() -> list[dict[str, Any]]:
    response = runner.application.test_client().get("/_instances")
    assert response.status_code == 200
    return response.get_json()["instances"]


def test_the_daemon_names_itself_after_its_manifest() -> None:
    manifest = load_manifest(_APP_MANIFEST_PATH)
    assert manifest.name == runner.APP_NAME
    assert manifest.instances is True
    assert manifest.instances_url is None
    assert [action.id for action in manifest.actions] == ["new"]


def test_list_answers_503_until_the_fleet_has_restored() -> None:
    runner._init_done.clear()

    response = runner.application.test_client().get("/_instances")

    assert response.status_code == 503
    assert "still restoring" in response.get_json()["detail"]


def test_list_reports_each_browser_with_status_from_its_ownership() -> None:
    assert _instances() == []
    fake = _install_running_browser("browser-2")

    assert _instances() == [
        {
            "key": "browser-2",
            "url": "/?session=browser-2",
            "title": "Browser 2",
            "status": "idle",
            "lifetime": "explicit",
            "last_active": None,
            "renameable": False,
        }
    ]
    assert runner.bridge.run(fake.acquire("A", "Alice"), timeout=5) == "acquired"
    assert _instances()[0]["status"] == "working"
    assert runner.bridge.run(fake.release("A"), timeout=5) is True
    assert _instances()[0]["status"] == "idle"
    fake._crashed = True
    assert _instances()[0]["status"] == "error"


def test_rename_and_a_rooted_location_are_refused() -> None:
    _install_running_browser("browser-1")
    client = runner.application.test_client()

    renamed = client.post("/_instances/browser-1/rename", json={"title": "Research"})
    relocated = client.post("/_instances/browser-1/location", json={"path": "/docs/"})

    assert renamed.status_code == 400
    assert relocated.status_code == 400
    assert "absolute http" in relocated.get_json()["detail"]


def test_location_is_a_conflict_while_an_agent_holds_the_browser() -> None:
    fake = _install_running_browser("browser-1")
    runner.bridge.run(fake.acquire("A", "Alice"), timeout=5)

    response = runner.application.test_client().post(
        "/_instances/browser-1/location", json={"path": "https://example.com/"}
    )

    assert response.status_code == 409
    assert "held by Alice" in response.get_json()["detail"]


def test_location_of_a_browser_the_fleet_does_not_hold_is_a_404() -> None:
    _install_running_browser("browser-1")

    response = runner.application.test_client().post(
        "/_instances/browser-2/location", json={"path": "https://example.com/"}
    )

    assert response.status_code == 404
    assert "browser-2" in response.get_json()["detail"]


def test_delete_closes_the_browser_and_nudges_even_for_an_unknown_key() -> None:
    nudger = RecordingNudger()
    runner.manager.set_nudger(nudger)
    _install_running_browser("browser-1")
    client = runner.application.test_client()

    closed = client.delete("/_instances/browser-1")
    unknown = client.delete("/_instances/browser-7")

    assert closed.status_code == 204
    assert unknown.status_code == 204
    assert _instances() == []
    assert runner.manager.has_browser("browser-1") is False
    # The route nudges once per call, and the manager once more for the browser it dropped.
    assert nudger.nudge_count == 3


def test_create_answers_409_with_the_install_reason_while_chromium_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BROWSER_SKIP_INSTALL_CHECK", raising=False)
    is_installed, reason = bsession.deferred_install_ready()
    if is_installed:
        pytest.skip(
            "Chromium is installed here, so the fleet would launch a real browser"
        )

    response = runner.application.test_client().post(
        "/_instances", json={"action": "new", "params": {}}
    )

    assert response.status_code == 409
    assert response.get_json()["detail"] == reason
    assert _instances() == []
