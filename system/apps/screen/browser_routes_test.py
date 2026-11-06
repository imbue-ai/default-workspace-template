"""The fleet API and the browser's launch path over the service fixture: ``POST /launch/browser`` starts the stack
and Chromium and answers the Screen's page, ``open:url`` opens or holds a page and shows the Screen, and the
``/browsers`` verbs answer as the fleet CLI expects."""

import pytest
from flask.testing import FlaskClient

from mock_chromium_test import FakeCdpConnector
from mock_chromium_test import FakeChromiumLauncher
from mock_window_tools_test import FakeWindowTools
from screen.data_types import BrowserLifecycle
from screen.data_types import Lifecycle
from screen.data_types import XWindow
from screen.primitives import BrowserName
from screen.primitives import XWindowId
from screen.service import ScreenService
from testing import start_browser
from testing import start_service_stack
from testing import wait_until
from workspace_layout.testing import FAKE_WINDOW_ID
from workspace_layout.testing import LoopbackShell
from workspace_layout.testing import desktop_answer
from workspace_layout.testing import fake_desktop

_AGENT_A = {"X-Mngr-Agent-Id": "chat-a", "X-Mngr-Agent-Name": "Alice"}
_AGENT_B = {"X-Mngr-Agent-Id": "chat-b", "X-Mngr-Agent-Name": "Bob"}
_CHROMIUM_WINDOW = XWindow(
    id=XWindowId("0x0000002"),
    desktop=0,
    pid=2,
    x=0,
    y=0,
    width=800,
    height=600,
    window_class="tilion.Tilion",
    title="Example",
)


def test_the_browser_launch_path_starts_the_stack_and_chromium_and_answers_the_screens_page(
    client: FlaskClient,
    service: ScreenService,
    fake_chromium_launcher: FakeChromiumLauncher,
    fake_cdp_connector: FakeCdpConnector,
) -> None:
    answer = client.post(
        "/launch/browser", json={"url": "https://example.com", "client_id": "c1", "desktop_id": "home"}
    )
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json() == {"path": "/"}
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING)
    browser = service.fleet.the_browser()
    assert browser is not None and browser.name == "browser-1"
    assert wait_until(lambda: browser.is_running)
    assert fake_chromium_launcher.requests[0].start_url == "https://example.com"
    state = client.get("/api/state").get_json()
    assert state["chromium"] == {"state": "running", "name": "browser-1", "pending_url": None}
    # A second launch with a page opens it as a tab in the browser already up, raising Chromium's window.
    tools = service.window_tools
    assert isinstance(tools, FakeWindowTools)
    tools.windows = [_CHROMIUM_WINDOW]
    again = client.post("/launch/browser", json={"url": "https://two.example"})
    assert again.status_code == 200 and again.get_json() == {"path": "/"}
    assert fake_cdp_connector.clients[0].created == ["https://two.example"]
    assert tools.activated == ["0x0000002"]
    assert len(fake_chromium_launcher.requests) == 1


def test_the_launch_path_refuses_a_bad_start_page_under_detail(client: FlaskClient) -> None:
    refused = client.post("/launch/browser", json={"url": "ftp://example.com"})
    assert refused.status_code == 400
    assert "url" in refused.get_json()["detail"]
    assert client.post("/launch/browser", json=["https://example.com"]).status_code == 400
    assert client.post("/launch/browser", json={"url": 3}).status_code == 400
    assert client.get("/launch/browser").status_code == 405


def test_open_url_opens_the_page_in_front_and_shows_the_screen_to_the_client_that_asked(
    client: FlaskClient, service: ScreenService, fake_cdp_connector: FakeCdpConnector, loopback_shell: LoopbackShell
) -> None:
    start_browser(client, service)
    loopback_shell.op_answer = {**desktop_answer(fake_desktop("home"), "c-link", FAKE_WINDOW_ID), "shown": "raised"}
    answer = client.post(
        "/api/open-url", json={"type": "open:url", "client_id": "c-link", "url": "http://localhost:3000/app"}
    )
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json() == {"browser": "browser-1", "window_id": FAKE_WINDOW_ID, "shown": "raised"}
    assert fake_cdp_connector.clients[0].created == ["http://localhost:3000/app"]
    assert loopback_shell.posted_ops() == [("show", {"app": "screen", "path": "/", "client": "c-link"})]


def test_open_url_holds_the_page_while_an_agent_drives_and_the_viewer_can_cancel_it(
    client: FlaskClient, service: ScreenService, fake_cdp_connector: FakeCdpConnector, loopback_shell: LoopbackShell
) -> None:
    name = start_browser(client, service)
    assert client.post(f"/browsers/{name}/acquire", json={}, headers=_AGENT_A).get_json()["ok"] is True
    held = client.post("/api/open-url", json={"client_id": "c-link", "url": "http://localhost:3000/"})
    assert held.status_code == 200, held.get_json()
    assert client.get("/api/state").get_json()["chromium"]["pending_url"] == "http://localhost:3000/"
    assert [op for op, _ in loopback_shell.posted_ops()] == ["show"]
    assert client.post(f"/browsers/{name}/pending-url/cancel").get_json() == {"cancelled": True}
    assert client.post(f"/browsers/{name}/pending-url/cancel").get_json() == {"cancelled": False}
    assert fake_cdp_connector.clients[0].created == []
    assert client.get("/api/state").get_json()["control"]["controller"] == "agent"


def test_the_launch_paths_start_page_is_held_while_an_agent_drives_but_an_agents_new_opens_at_once(
    client: FlaskClient, service: ScreenService, fake_cdp_connector: FakeCdpConnector
) -> None:
    name = start_browser(client, service)
    tools = service.window_tools
    assert isinstance(tools, FakeWindowTools)
    tools.windows = [_CHROMIUM_WINDOW]
    assert client.post(f"/browsers/{name}/acquire", json={}, headers=_AGENT_A).get_json()["ok"] is True
    held = client.post("/launch/browser", json={"url": "https://example.com", "client_id": "c1"})
    assert held.status_code == 200 and held.get_json() == {"path": "/"}
    assert client.get("/api/state").get_json()["chromium"]["pending_url"] == "https://example.com"
    assert fake_cdp_connector.clients[0].created == []
    # Chromium is not raised over the agent's work for a page that waits for the human.
    assert tools.activated == []
    opened = client.post("/browsers", json={"url": "https://agent.example"}, headers=_AGENT_A)
    assert opened.status_code == 200 and opened.get_json() == {"name": name}
    assert fake_cdp_connector.clients[0].created == ["https://agent.example"]
    assert tools.activated == ["0x0000002"]


def test_a_held_page_opens_once_the_agent_releases(
    client: FlaskClient, service: ScreenService, fake_cdp_connector: FakeCdpConnector, loopback_shell: LoopbackShell
) -> None:
    name = start_browser(client, service)
    client.post(f"/browsers/{name}/acquire", json={}, headers=_AGENT_A)
    client.post("/api/open-url", json={"client_id": "c-link", "url": "http://localhost:3000/"})
    assert client.post(f"/browsers/{name}/release", headers=_AGENT_A).get_json() == {"released": True}
    assert wait_until(lambda: fake_cdp_connector.clients[0].created == ["http://localhost:3000/"])
    assert client.get("/api/state").get_json()["chromium"]["pending_url"] is None


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param(
            {"client_id": "c-link", "url": "https://example.com/"}, "only an address on this machine", id="external"
        ),
        pytest.param({"client_id": "c-link", "url": "ftp://localhost/"}, "absolute http or https URL", id="not-http"),
        pytest.param({"client_id": "c-link", "url": 7}, "field 'url'", id="not-a-string"),
        pytest.param({"client_id": "not a client", "url": "http://localhost:3000/"}, "client_id", id="bad-client"),
    ],
)
def test_open_url_refuses_what_it_cannot_open_with_the_reason_under_detail(
    client: FlaskClient, loopback_shell: LoopbackShell, body: dict[str, object], detail: str
) -> None:
    answer = client.post("/api/open-url", json=body)
    assert answer.status_code == 400
    assert detail in answer.get_json()["detail"]
    assert loopback_shell.posted_ops() == []


def test_open_url_says_why_when_the_shell_will_not_show_the_screen(
    client: FlaskClient, service: ScreenService, fake_cdp_connector: FakeCdpConnector, loopback_shell: LoopbackShell
) -> None:
    start_browser(client, service)
    loopback_shell.op_refusal = (404, {"detail": "No client 'c-gone'"})
    answer = client.post("/api/open-url", json={"client_id": "c-gone", "url": "http://localhost:3000/"})
    assert answer.status_code == 502
    assert "No client 'c-gone'" in answer.get_json()["detail"]
    assert fake_cdp_connector.clients[0].created == ["http://localhost:3000/"]


def test_the_fleet_is_listed_with_the_lease_and_the_tabs(client: FlaskClient, service: ScreenService) -> None:
    empty = client.get("/browsers").get_json()
    assert empty["browsers"] == [] and empty["can_create"] is True and empty["browser_max"] == 1
    name = start_browser(client, service)
    listed = client.get("/browsers").get_json()
    assert listed["browser_count"] == 1
    assert listed["browsers"][0]["id"] == name
    assert listed["browsers"][0]["lifecycle"] == "running"
    assert listed["browsers"][0]["controller"] == "nobody"


def test_a_named_create_and_its_refusals(client: FlaskClient, service: ScreenService) -> None:
    created = client.post("/browsers", json={"name": "research", "url": "https://example.com"})
    assert created.status_code == 200 and created.get_json() == {"name": "research"}
    assert wait_until(lambda: service.fleet.get(BrowserName("research")).is_running)
    assert client.post("/browsers", json={"name": "Bad Name"}).status_code == 400
    assert client.post("/browsers", json={"name": "second"}).status_code == 409
    assert client.post("/browsers", json={"url": "nope"}).status_code == 400


def test_attach_acquire_release_and_handoff_through_the_fleet_routes(
    client: FlaskClient, service: ScreenService
) -> None:
    name = start_browser(client, service)
    assert client.get(f"/browsers/{name}/attach").status_code == 400
    attached = client.get(f"/browsers/{name}/attach", headers=_AGENT_A).get_json()
    assert attached["ok"] is True and attached["attach_url"].startswith("http://127.0.0.1:")
    acquired = client.post(f"/browsers/{name}/acquire", json={}, headers=_AGENT_A).get_json()
    assert acquired["ok"] is True and acquired["controller"] == "agent" and acquired["lifecycle"] == "running"
    busy = client.post(f"/browsers/{name}/acquire", json={}, headers=_AGENT_B).get_json()
    assert busy["ok"] is False and busy["status"] == "busy_agent" and busy["enqueued"] is True
    assert client.get(f"/browsers/{name}/attach", headers=_AGENT_B).get_json()["status"] == "busy_agent"
    handed = client.post(f"/browsers/{name}/handoff", json={"reason": "a CAPTCHA"}, headers=_AGENT_A).get_json()
    assert handed["ok"] is True and handed["status"] == "handed_off"
    assert client.post(f"/browsers/{name}/release", headers=_AGENT_A).get_json() == {"released": False}
    assert client.get("/browsers/nobody/attach", headers=_AGENT_A).status_code == 404
    assert client.get("/browsers/Bad%20Name/attach", headers=_AGENT_A).status_code == 404


def test_acquire_on_a_browser_still_launching_says_starting(
    client: FlaskClient, service: ScreenService, fake_chromium_launcher: FakeChromiumLauncher
) -> None:
    fake_chromium_launcher.release.clear()
    start_service_stack(service)
    name = client.post("/browsers", json={}).get_json()["name"]
    answer = client.post(f"/browsers/{name}/acquire", json={}, headers=_AGENT_A).get_json()
    assert answer["ok"] is False and answer["status"] == "starting"
    fake_chromium_launcher.release.set()
    assert wait_until(lambda: service.fleet.get(BrowserName(name)).is_running)


def test_stop_start_close_tab_and_delete_through_the_routes(
    client: FlaskClient, service: ScreenService, fake_cdp_connector: FakeCdpConnector
) -> None:
    name = start_browser(client, service)
    cdp = fake_cdp_connector.clients[0]
    cdp.create_target("https://one.example")
    cdp.create_target("https://two.example")
    cdp.shown_target_id = "new-2"
    assert client.post(f"/browsers/{name}/close-tab").get_json() == {"closed": True}
    assert cdp.closed == ["new-2"]
    assert client.post(f"/browsers/{name}/stop").get_json() == {"stopped": True}
    assert service.fleet.get(BrowserName(name)).lifecycle == BrowserLifecycle.STOPPED
    assert client.post(f"/browsers/{name}/close-tab").status_code == 409
    assert client.post(f"/browsers/{name}/start").get_json() == {"started": True}
    assert wait_until(lambda: service.fleet.get(BrowserName(name)).is_running)
    assert client.delete(f"/browsers/{name}").get_json() == {"closed": True}
    assert service.fleet.browsers() == []
    assert client.post("/browsers/nobody/stop").status_code == 404


def test_a_stack_stop_checkpoints_the_tabs_and_the_browser_comes_back_with_the_stack(
    client: FlaskClient,
    service: ScreenService,
    fake_cdp_connector: FakeCdpConnector,
    fake_chromium_launcher: FakeChromiumLauncher,
) -> None:
    name = start_browser(client, service)
    fake_cdp_connector.clients[0].create_target("https://kept.example")
    service.stack.stop("quit")
    manifest = service.fleet.manifest_store.read()
    assert manifest is not None
    assert manifest.browsers[0].tabs == ("https://kept.example",) and manifest.browsers[0].stopped is False
    assert service.fleet.get(BrowserName(name)).lifecycle == BrowserLifecycle.STOPPED
    assert client.get("/api/state").get_json()["chromium"]["state"] == "stopped"
    start_service_stack(service)
    assert wait_until(
        lambda: service.fleet.get(BrowserName(name)).is_running and len(fake_chromium_launcher.requests) == 2
    )
    assert fake_chromium_launcher.requests[1].start_url == "https://kept.example"
