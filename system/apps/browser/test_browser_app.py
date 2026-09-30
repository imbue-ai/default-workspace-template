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
from mock_cdp_client_test import TabClosingCdpClient
from workspace_layout.testing import FAKE_WINDOW_ID, LoopbackShell, desktop_answer

# The manifest the supervisord program line registers with ``forward_port.py --manifest``.
_APP_MANIFEST_PATH = Path(__file__).parent / "app.toml"


def test_the_daemon_names_itself_after_its_manifest() -> None:
    manifest = load_manifest(_APP_MANIFEST_PATH)
    assert manifest.name == APP_NAME
    assert [launch_path.path for launch_path in manifest.launch_paths] == [runner.NEW_PATH]
    assert manifest.window_closed_path == runner.WINDOW_CLOSED_PATH
    assert [(handler.type, handler.path, handler.show) for handler in manifest.message_handlers] == [
        ("open:url", runner.OPEN_URL_PATH, None)
    ]


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


def test_new_creates_a_browser_and_answers_its_page_path(monkeypatch: pytest.MonkeyPatch) -> None:
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

    # The reason rides under ``detail``, the key the shell's launch route passes on as the app's refusal.
    assert response.status_code == 400
    assert "url" in response.get_json()["detail"]
    assert "error" not in response.get_json()


def test_new_refuses_a_body_that_is_not_a_json_object_and_a_get(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    client = runner.application.test_client()

    not_an_object = client.post("/new", json=["https://example.com"])
    assert not_an_object.status_code == 400
    assert "JSON object" in not_an_object.get_json()["detail"]
    assert client.post("/new", data="").status_code == 400
    not_a_string = client.post("/new", json={"url": 3})
    assert not_a_string.status_code == 400
    assert "url" in not_a_string.get_json()["detail"]
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


def test_close_tab_closes_the_shown_tab_of_a_running_browser_and_refuses_the_rest() -> None:
    running = bsession.LiveBrowser(browser_id="browser-1")
    running._lifecycle = "running"
    cdp = TabClosingCdpClient(
        [{"targetId": "t1", "url": "https://one.example"}, {"targetId": "t2", "url": "https://two.example"}],
        shown_target_id="t1",
    )
    running._cdp = cdp
    running._active_target_id = "t1"
    runner.manager._browsers["browser-1"] = running
    runner.manager._browsers["browser-2"] = bsession.LiveBrowser(browser_id="browser-2")
    client = runner.application.test_client()

    closed = client.post("/browsers/browser-1/close-tab")
    still_launching = client.post("/browsers/browser-2/close-tab")
    unknown = client.post("/browsers/browser-9/close-tab")

    assert closed.status_code == 200, closed.text
    assert closed.get_json() == {"closed": True}
    assert cdp.closed == ["t1"]
    assert running._active_target_id == "t2"
    assert still_launching.status_code == 409
    assert "not running" in still_launching.get_json()["error"]
    assert unknown.status_code == 404


def test_the_viewer_module_script_reads_no_state_from_the_classic_script() -> None:
    """The shell-contract block is a module: it cannot see the viewer closure's names."""
    page = (Path(__file__).parent / "src" / "browser" / "assets" / "index.html").read_text()
    module_block = page.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    assert "browserId" not in module_block
    assert '"browsers/" + session + "/close-tab"' in module_block


# ``POST /api/open-url``, the ``open:url`` message handler: a local page the human asked for


def _running_browser_one() -> tuple[bsession.LiveBrowser, TabClosingCdpClient]:
    browser = bsession.LiveBrowser(browser_id="browser-1")
    browser._lifecycle = "running"
    cdp = TabClosingCdpClient([{"targetId": "t1", "url": "https://one.example"}], shown_target_id="t1")
    browser._cdp = cdp
    browser._active_target_id = "t1"
    runner.manager._browsers["browser-1"] = browser
    return browser, cdp


def _showing(shell: LoopbackShell, shown: str) -> None:
    shell.op_answer = {**desktop_answer("home", "c-link", [], FAKE_WINDOW_ID, []), "shown": shown}


def test_open_url_opens_the_page_in_front_and_shows_the_browser_to_the_client_that_asked(
    monkeypatch: pytest.MonkeyPatch, loopback_shell: LoopbackShell
) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    _, cdp = _running_browser_one()
    _showing(loopback_shell, "raised")

    response = runner.application.test_client().post(
        runner.OPEN_URL_PATH, json={"type": "open:url", "client_id": "c-link", "url": "http://localhost:3000/app"}
    )

    assert response.status_code == 200, response.text
    assert response.get_json() == {"browser": "browser-1", "window_id": FAKE_WINDOW_ID, "shown": "raised"}
    assert cdp.created == ["http://localhost:3000/app"]
    assert loopback_shell.posted_ops() == [
        ("show", {"app": "browser", "path": "/?session=browser-1", "showing": [], "repoint": [], "client": "c-link"})
    ]


def test_open_url_holds_the_page_while_an_agent_drives_and_the_viewer_can_cancel_it(
    monkeypatch: pytest.MonkeyPatch, loopback_shell: LoopbackShell
) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    browser, cdp = _running_browser_one()
    _showing(loopback_shell, "raised")
    assert runner.bridge.run(browser.acquire("agent-7", "Plan"), timeout=5) == "acquired"
    client = runner.application.test_client()

    held = client.post(runner.OPEN_URL_PATH, json={"client_id": "c-link", "url": "http://localhost:3000/"})
    cancelled = client.post("/browsers/browser-1/pending-url/cancel")
    cancelled_again = client.post("/browsers/browser-1/pending-url/cancel")

    assert held.status_code == 200, held.text
    assert [op for op, _ in loopback_shell.posted_ops()] == ["show"]
    assert cancelled.get_json() == {"cancelled": True}
    assert cancelled_again.get_json() == {"cancelled": False}
    assert cdp.created == []
    assert browser._state_tuple() == ("agent", "agent-7", False)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param({"client_id": "c-link", "url": "https://example.com/"}, "only an address on this machine", id="external"),
        pytest.param({"client_id": "c-link", "url": "ftp://localhost/"}, "absolute http or https URL", id="not-http"),
        pytest.param({"client_id": "c-link", "url": 7}, "url: must be a string", id="not-a-string"),
        pytest.param({"client_id": "not a client", "url": "http://localhost:3000/"}, "client_id", id="bad-client"),
    ],
)
def test_open_url_refuses_what_it_cannot_open_with_the_reason_under_detail(
    monkeypatch: pytest.MonkeyPatch, loopback_shell: LoopbackShell, body: dict[str, object], detail: str
) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    _, cdp = _running_browser_one()

    response = runner.application.test_client().post(runner.OPEN_URL_PATH, json=body)

    assert response.status_code == 400
    assert detail in response.get_json()["detail"]
    assert cdp.created == []
    assert loopback_shell.posted_ops() == []


def test_open_url_says_why_when_the_shell_will_not_show_the_browser(
    monkeypatch: pytest.MonkeyPatch, loopback_shell: LoopbackShell
) -> None:
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    _, cdp = _running_browser_one()
    loopback_shell.op_refusal = (404, {"detail": "No client 'c-gone'"})

    response = runner.application.test_client().post(
        runner.OPEN_URL_PATH, json={"client_id": "c-gone", "url": "http://localhost:3000/"}
    )

    assert response.status_code == 502
    assert "No client 'c-gone'" in response.get_json()["detail"]
    assert cdp.created == ["http://localhost:3000/"]
