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

# The manifest the supervisord program line registers with ``forward_port.py --manifest``.
_APP_MANIFEST_PATH = Path(__file__).parent / "app.toml"


def test_the_daemon_names_itself_after_its_manifest() -> None:
    manifest = load_manifest(_APP_MANIFEST_PATH)
    assert manifest.name == APP_NAME
    assert [launch_path.path for launch_path in manifest.launch_paths] == [runner.NEW_PATH]


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
