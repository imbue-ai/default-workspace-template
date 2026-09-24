"""The daemon's pages as the workspace shell reaches them: the viewer page and the ``new``
launch path, served by the real Flask app over the real manager and bridge (started once by
the conftest); the one create captures the launch it would spawn instead of starting Chromium."""

from pathlib import Path

import pytest
from app_manifest.manifest import load_manifest
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from browser import runner
from browser import session as bsession
from browser.primitives import APP_NAME
from imbue.mngr.utils.polling import wait_for

# The manifest the supervisord program line registers with ``forward_port.py --manifest``.
_APP_MANIFEST_PATH = Path(__file__).parent / "app.toml"


def test_the_daemon_names_itself_after_its_manifest() -> None:
    manifest = load_manifest(_APP_MANIFEST_PATH)
    assert manifest.name == APP_NAME
    assert [launch_path.path for launch_path in manifest.launch_paths] == [runner.NEW_PATH]
    assert manifest.window_closed_path == runner.WINDOW_CLOSED_PATH


def test_the_viewer_page_imports_the_app_contract_from_its_own_origin() -> None:
    response = runner.application.test_client().get("/")

    assert response.status_code == 200
    assert 'import("/_static/app_contract.js")' in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_the_app_contract_module_is_the_shells_build_output_served_from_this_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The path is relative to the repo root every supervised program runs from.
    monkeypatch.chdir(tmp_path)
    missing = runner.application.test_client().get("/_static/app_contract.js")
    assert missing.status_code == 404
    assert "not built" in missing.get_json()["error"]

    source = "export function connectToShell() {}\n"
    built = tmp_path / SHELL_APP_CONTRACT_PATH
    built.parent.mkdir(parents=True)
    built.write_text(source)
    served = runner.application.test_client().get("/_static/app_contract.js")

    assert served.status_code == 200
    assert served.mimetype == "text/javascript"
    assert served.text == source


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

    # The shell's envelope fields ride beside the param and are ignored.
    response = runner.application.test_client().post(
        "/new", json={"url": "https://example.com", "client_id": "client-1", "desktop_id": "home"}
    )

    assert response.status_code == 200, response.text
    name = response.get_json()["path"].removeprefix("/?session=")
    assert name in runner.manager._browsers
    assert response.get_json() == {"path": f"/?session={name}"}
    assert launched == [(runner.manager._browsers[name], ["https://example.com"])]


def test_new_refuses_a_start_page_that_is_not_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")

    response = runner.application.test_client().post("/new", json={"url": "ftp://example.com"})

    assert response.status_code == 400
    assert "url" in response.get_json()["error"]


def test_new_refuses_a_body_that_is_not_a_json_object_and_a_get(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    client = runner.application.test_client()

    assert client.post("/new", json=["https://example.com"]).status_code == 400
    assert client.post("/new", data="").status_code == 400
    assert client.get("/new").status_code == 405


def test_new_answers_the_browser_already_up_without_another_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    launched: list[bsession.LiveBrowser] = []
    monkeypatch.setattr(
        bsession.BrowserSessionManager, "_spawn_launch", lambda self, session, **k: launched.append(session)
    )
    up = bsession.LiveBrowser(browser_id="browser-1")
    up._lifecycle = "running"
    runner.manager._browsers["browser-1"] = up

    launched_path = runner.application.test_client().post("/new", json={})
    created = runner.application.test_client().post("/browsers", json={})

    assert launched_path.status_code == 200, launched_path.text
    assert launched_path.get_json() == {"path": "/?session=browser-1"}
    assert created.get_json() == {"name": "browser-1"}
    assert launched == []


def test_a_window_closed_post_sweeps_at_once_and_answers_no_content(monkeypatch: pytest.MonkeyPatch) -> None:
    swept: list[str] = []

    async def fake_sweep(self: bsession.BrowserSessionManager, shell_url: str) -> list[str] | None:
        swept.append(shell_url)
        return []

    monkeypatch.setattr(bsession.BrowserSessionManager, "sweep_from_shell", fake_sweep)

    response = runner.application.test_client().post(
        "/api/window-closed", json={"path": "/?session=browser-1", "window_id": "win-1", "desktop_id": "home"}
    )

    assert response.status_code == 204
    # The route only schedules the sweep on the bridge loop, so the answer can land before the sweep runs.
    wait_for(lambda: len(swept) == 1, timeout=5.0, poll_interval=0.02, error_message="the hinted sweep never ran")
