"""The daemon's pages as the workspace shell reaches them: the viewer page and the ``new``
launch path, served by the real Flask app over the real manager and bridge (started once by
the conftest), with fake in-memory browsers standing in for Chromium."""

from pathlib import Path

import pytest
from app_manifest.manifest import load_manifest
from browser import runner
from browser.primitives import APP_NAME
from browser import session as bsession

# The manifest the supervisord program line registers with ``forward_port.py --manifest``.
_APP_MANIFEST_PATH = Path(__file__).parent / "app.toml"


def test_the_daemon_names_itself_after_its_manifest() -> None:
    manifest = load_manifest(_APP_MANIFEST_PATH)
    assert manifest.name == APP_NAME
    assert [launch_path.path for launch_path in manifest.launch_paths] == [runner.NEW_PATH]


def test_the_viewer_page_carries_the_shells_origin_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\nlabel = "system_interface-a1b2"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))

    response = runner.application.test_client().get("/")

    assert response.status_code == 200
    assert '<meta name="workspace-shell-label" content="system_interface-a1b2">' in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_new_creates_a_browser_and_redirects_to_its_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    # The launch is captured rather than run, so the test stays Chromium-free; what matters
    # here is that the create registered the browser and asked for its start page.
    launched: list[tuple[bsession.LiveBrowser, list[str] | None]] = []
    monkeypatch.setattr(
        bsession.BrowserSessionManager,
        "_spawn_launch",
        lambda self, session, restore_tabs=None, **k: launched.append((session, restore_tabs)),
    )

    response = runner.application.test_client().get("/new?url=https://example.com", follow_redirects=False)

    assert response.status_code == 302, response.text
    name = response.headers["Location"].removeprefix("/?session=")
    assert name in runner.manager._browsers
    assert response.headers["Location"] == f"/?session={name}"
    assert launched == [(runner.manager._browsers[name], ["https://example.com"])]


def test_new_refuses_a_start_page_that_is_not_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")

    response = runner.application.test_client().get("/new?url=ftp://example.com", follow_redirects=False)

    assert response.status_code == 400
    assert "url" in response.get_json()["error"]
