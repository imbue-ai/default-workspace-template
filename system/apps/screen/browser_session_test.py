"""The ``driven_browser`` block over the service fixture on a real listener and a stand-in ``playwright-cli``: what it
attaches, that it detaches and gives the lease back however its body ends, and how a refusal and a failed detach
surface."""

import socket
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from screen.browser_session import driven_browser
from screen.cli import ENV_SERVICE_URL
from screen.data_types import Controller
from screen.errors import BrowserSessionError
from screen.errors import BrowserUnavailableError
from screen.errors import InvalidSessionPurposeError
from screen.errors import ScreenServiceUnreachableError
from screen.primitives import AgentId
from screen.primitives import BrowserName
from screen.service import ScreenService
from testing import start_browser


class _BodyError(Exception):
    """What a test's block body raises."""


def _calls(cli_dir: Path) -> list[str]:
    log = cli_dir / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _is_leased(service: ScreenService, agent_id: AgentId) -> bool:
    view = service.control.view()
    return view.controller == Controller.AGENT and view.agent_id == agent_id


def test_the_block_drives_a_session_of_its_own_and_detaches_it_and_releases_however_the_body_ends(
    service: ScreenService,
    client: FlaskClient,
    served_screen: str,
    fake_playwright_cli: Path,
    script_agent: AgentId,
) -> None:
    name = start_browser(client, service)
    browser = service.fleet.get(BrowserName(name))
    with driven_browser(browser_name=name, purpose="sync") as driven:
        assert _is_leased(service, script_agent)
        driven.run("goto", "https://example.com")
        session = driven.session_name
        token = browser.token
    assert session.startswith("sync-")
    assert _calls(fake_playwright_cli) == [
        f"-s={session} attach --cdp={service.fleet.proxy_server.attach_url(name, token)}",
        f"-s={session} goto https://example.com",
        f"-s={session} detach",
    ]
    assert not _is_leased(service, script_agent)
    with pytest.raises(_BodyError):
        with driven_browser(browser_name=name, purpose="sync") as driven:
            raised_session = driven.session_name
            raise _BodyError("the body failed")
    assert raised_session != session
    assert _calls(fake_playwright_cli)[-1] == f"-s={raised_session} detach"
    assert not _is_leased(service, script_agent)


def test_a_browser_another_agent_holds_is_refused_with_nothing_attached(
    service: ScreenService,
    client: FlaskClient,
    served_screen: str,
    fake_playwright_cli: Path,
    script_agent: AgentId,
) -> None:
    name = start_browser(client, service)
    held = client.post(f"/browsers/{name}/acquire", json={}, headers={"X-Mngr-Agent-Id": "chat-other"})
    assert held.get_json()["ok"] is True, held.get_json()
    with pytest.raises(BrowserUnavailableError) as refused:
        with driven_browser(browser_name=name, purpose="sync"):
            pytest.fail("the body ran on a browser another agent holds")
    assert refused.value.status == "busy_agent"
    assert _calls(fake_playwright_cli) == []


def test_a_purpose_the_reaper_could_not_recognize_is_refused_before_the_browser_is_reserved(
    service: ScreenService,
    client: FlaskClient,
    served_screen: str,
    fake_playwright_cli: Path,
    script_agent: AgentId,
) -> None:
    name = start_browser(client, service)
    for purpose in ("", "-sync", "plaud sync", "plaud/sync"):
        with pytest.raises(InvalidSessionPurposeError):
            with driven_browser(browser_name=name, purpose=purpose):
                pytest.fail(f"the body ran with purpose {purpose!r}")
        assert not _is_leased(service, script_agent), purpose
    assert _calls(fake_playwright_cli) == []


def test_a_failed_detach_fails_a_clean_block_but_gives_way_to_the_bodys_own_error(
    service: ScreenService,
    client: FlaskClient,
    served_screen: str,
    fake_playwright_cli: Path,
    script_agent: AgentId,
) -> None:
    name = start_browser(client, service)
    (fake_playwright_cli / "fail-detach").touch()
    with pytest.raises(BrowserSessionError, match="could not detach"):
        with driven_browser(browser_name=name, purpose="sync"):
            pass
    assert not _is_leased(service, script_agent)
    with pytest.raises(_BodyError):
        with driven_browser(browser_name=name, purpose="sync"):
            raise _BodyError("the body failed")
    assert not _is_leased(service, script_agent)


def test_a_screen_service_that_does_not_answer_raises_unreachable_with_nothing_attached(
    fake_playwright_cli: Path, script_agent: AgentId, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setenv(ENV_SERVICE_URL, f"http://127.0.0.1:{closed_port}")
    with pytest.raises(ScreenServiceUnreachableError):
        with driven_browser(purpose="sync"):
            pytest.fail("the body ran with no Screen service")
    assert _calls(fake_playwright_cli) == []
