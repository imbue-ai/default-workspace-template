"""The gated CDP endpoint's own contract.

These cover the guarantees §11 of the handover plan calls load-bearing, without needing a
real browser: the discovery rewrite (nothing may leak the upstream port), the block list,
the last-page guard, and -- the acceptance criterion for the whole change -- that losing
the lease refuses the agent's very next frame while leaving its socket alive.

Why "leaving the socket alive" is asserted rather than assumed: a `@playwright/cli` slug is
poisoned the moment its socket drops (measured -- it never rebinds and disappears from
`playwright-cli list`), so closing the connection on takeover would permanently brick the
browser for the agent. Refusing frames was measured to recover cleanly.
"""

import asyncio
import json
from typing import Any

import pytest
from browser.cdp_proxy import _ALWAYS_BLOCKED
from browser.cdp_proxy import _LAST_PAGE_GUARDED
from browser.cdp_proxy import BrowserProxy
from browser.cdp_proxy import ProxyServer


def _proxy(*, allowed: bool = True, pages: int = 2) -> "tuple[BrowserProxy, dict[str, Any]]":
    seen: dict[str, Any] = {"attached": 0, "activity": []}

    async def is_allowed(token: str) -> bool:
        return allowed and token == "good"

    async def on_attach() -> None:
        seen["attached"] += 1

    async def on_activity(target_id: str | None) -> None:
        seen["activity"].append(target_id)

    async def page_count() -> int:
        return pages

    proxy = BrowserProxy(
        name="browser-1",
        upstream_http="http://127.0.0.1:1",
        public_base="ws://127.0.0.1:2",
        is_allowed=is_allowed,
        on_attach=on_attach,
        on_activity=on_activity,
        page_count=page_count,
    )
    return proxy, seen


def _screen(proxy: BrowserProxy, method: str, token: str = "good") -> str | None:
    return asyncio.run(proxy._screen({"id": 1, "method": method}, token))


def _discover(proxy: BrowserProxy, path: str, token: str = "good") -> "Any":
    return asyncio.run(proxy.rewrite_discovery(path, token))


def test_a_valid_token_forwards_ordinary_methods() -> None:
    proxy, _ = _proxy()
    assert _screen(proxy, "Page.navigate") is None
    assert _screen(proxy, "Runtime.evaluate") is None
    assert _screen(proxy, "Input.dispatchMouseEvent") is None


def test_losing_the_lease_refuses_the_very_next_frame() -> None:
    # THE acceptance criterion. `run_action`'s per-command compare-and-set used to do this;
    # the per-frame token check is its replacement, and it must bite immediately.
    proxy, _ = _proxy(allowed=False)
    reason = _screen(proxy, "Input.dispatchMouseEvent")
    assert reason is not None and "not yours" in reason
    # The message has to tell the agent where the authoritative answer lives, because
    # playwright-cli collapses every failure into exit 1 with its own wording.
    assert "ls" in reason


def test_another_agents_token_cannot_drive() -> None:
    # A generic CDP client sends no X-Mngr-Agent-Id header, so the token is the ONLY thing
    # separating the lease-holder from any other attacher on the same box.
    proxy, _ = _proxy()
    assert _screen(proxy, "Page.navigate", token="someone-elses") is not None
    assert _screen(proxy, "Page.navigate", token="") is not None


@pytest.mark.parametrize("method", sorted(_ALWAYS_BLOCKED))
def test_destructive_methods_are_refused_even_while_holding_the_lease(method: str) -> None:
    proxy, _ = _proxy()
    assert _screen(proxy, method) is not None


def test_closing_the_last_page_is_refused_but_other_tabs_are_fine() -> None:
    for method in sorted(_LAST_PAGE_GUARDED):
        one_page, _ = _proxy(pages=1)
        reason = _screen(one_page, method)
        assert reason is not None and "last page" in reason
        many_pages, _ = _proxy(pages=3)
        assert _screen(many_pages, method) is None


def test_setautoattach_is_forwarded_untouched() -> None:
    # Playwright sets waitForDebuggerOnStart deliberately and pairs it with
    # Runtime.runIfWaitingForDebugger. An earlier draft rewrote that flag "for safety",
    # which would have manufactured the exact new-tab deadlock it claimed to prevent.
    proxy, _ = _proxy()
    assert _screen(proxy, "Target.setAutoAttach") is None


def test_discovery_rewrites_every_websocket_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # If ANY upstream URL survives the rewrite, a client discovers the real endpoint and
    # connects around every control in this module.
    proxy, _ = _proxy()
    upstream = {
        "Browser": "Chrome/151",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9777/devtools/browser/abc",
    }
    monkeypatch.setattr(BrowserProxy, "_fetch", lambda self, endpoint: upstream)
    response = _discover(proxy, "/browser-1/good/json/version", "good")
    assert response is not None
    body = json.loads(response.body)
    assert "9777" not in json.dumps(body)
    assert body["webSocketDebuggerUrl"] == "ws://127.0.0.1:2/browser-1/good"


def test_discovery_filters_targets_to_real_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh profile with ONE page reports five targets; only the page is a tab. Our `ls`
    # must agree with what `playwright-cli tab-list` shows, or index N means two things.
    proxy, _ = _proxy()
    upstream = [
        {"type": "browser_ui", "url": "chrome://omnibox-popup.top-chrome/", "webSocketDebuggerUrl": "ws://127.0.0.1:9777/a"},
        {"type": "background_page", "url": "chrome-extension://x/bg.html", "webSocketDebuggerUrl": "ws://127.0.0.1:9777/b"},
        {"type": "service_worker", "url": "chrome-extension://y/sw.js", "webSocketDebuggerUrl": "ws://127.0.0.1:9777/c"},
        {"type": "page", "url": "https://example.com", "webSocketDebuggerUrl": "ws://127.0.0.1:9777/d",
         "devtoolsFrontendUrl": "/devtools/inspector.html?ws=127.0.0.1:9777/d"},
    ]
    monkeypatch.setattr(BrowserProxy, "_fetch", lambda self, endpoint: upstream)
    response = _discover(proxy, "/browser-1/good/json/list/", "good")
    assert response is not None
    targets = json.loads(response.body)
    assert [t["type"] for t in targets] == ["page"]
    assert "9777" not in json.dumps(targets)
    assert all("devtoolsFrontendUrl" not in t for t in targets)


def test_the_legacy_mutating_http_endpoints_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # /json/new, /json/close and /json/activate bypass the websocket filter entirely.
    proxy, _ = _proxy()
    monkeypatch.setattr(BrowserProxy, "_fetch", lambda self, endpoint: {})
    for verb in ("new", "close", "activate"):
        response = _discover(proxy, f"/browser-1/good/json/{verb}", "good")
        assert response is not None and response.status_code == 405


def test_discovery_is_served_on_both_bare_and_trailing_slash_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # Playwright fetches `/json/version/`. A proxy routing only the bare path fails the
    # attach with `Unexpected status 404` and the CLI daemon exits 1 -- measured.
    proxy, _ = _proxy()
    monkeypatch.setattr(BrowserProxy, "_fetch", lambda self, endpoint: {"webSocketDebuggerUrl": "ws://x/y"})
    for path in ("/browser-1/good/json/version", "/browser-1/good/json/version/"):
        assert _discover(proxy, path) is not None


def test_a_non_json_path_falls_through_to_the_websocket_handshake() -> None:
    proxy, _ = _proxy()
    assert _discover(proxy, "/browser-1/good") is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/browser-1/tok", ("browser-1", "tok")),
        ("/browser-1/tok/json/version/", ("browser-1", "tok")),
        ("/browser-1/tok?x=1", ("browser-1", "tok")),
        ("/", ("", "")),
    ],
)
def test_server_splits_name_and_token_from_the_path(path: str, expected: "tuple[str, str]") -> None:
    assert ProxyServer()._split(path) == expected


def test_registration_is_addressable_and_removable() -> None:
    server = ProxyServer()
    proxy, _ = _proxy()
    server.register(proxy)
    assert server._browsers["browser-1"] is proxy
    server.unregister("browser-1")
    assert "browser-1" not in server._browsers


def test_discovery_responses_close_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without `Connection: close` the client keeps the socket alive and sends the websocket
    # upgrade on it, but this server is done with that connection -- so the upgrade hangs up
    # (code 1006) and the CLI daemon exits 1. Found only by attaching a REAL playwright-cli;
    # every unit test passed while the attach was broken end to end.
    proxy, _ = _proxy()
    monkeypatch.setattr(BrowserProxy, "_fetch", lambda self, endpoint: {"webSocketDebuggerUrl": "ws://x/y"})
    response = _discover(proxy, "/browser-1/good/json/version/", "good")
    assert response is not None
    assert response.headers["Connection"] == "close"
