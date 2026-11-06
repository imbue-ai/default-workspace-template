"""The gated CDP endpoint's contract (the browser app's cases carried over): the discovery rewrite (nothing may leak
the upstream port), the block list, the last-page guard, that losing the lease refuses the agent's very next frame
while leaving its socket alive, and one relay end to end through the real server."""

import json
import threading
from typing import Any

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as connect_websocket

from screen.cdp_proxy import ALWAYS_BLOCKED
from screen.cdp_proxy import BrowserProxy
from screen.cdp_proxy import LAST_PAGE_GUARDED
from screen.cdp_proxy import ProxyServer
from screen.cdp_proxy import split_proxy_path
from testing import FakeChromiumServer
from testing import wait_until


class _Seen:
    def __init__(self) -> None:
        self.attached = 0
        self.activity: list[str | None] = []
        self.activity_seen = threading.Event()


def _proxy(
    is_allowed: bool = True,
    pages: int = 2,
    upstream_http: str = "http://127.0.0.1:1",
    public_base: str = "ws://127.0.0.1:2",
) -> tuple[BrowserProxy, _Seen]:
    seen = _Seen()

    def on_activity(target_id: str | None) -> None:
        seen.activity.append(target_id)
        seen.activity_seen.set()

    def on_attach() -> None:
        seen.attached += 1

    proxy = BrowserProxy(
        name="browser-1",
        upstream_http=upstream_http,
        public_base=public_base,
        is_allowed=lambda token: is_allowed and token == "good",
        on_attach=on_attach,
        on_activity=on_activity,
        page_count=lambda: pages,
    )
    return proxy, seen


def _screen(proxy: BrowserProxy, method: str, token: str = "good") -> str | None:
    return proxy.screen({"id": 1, "method": method}, token)


def test_a_valid_token_forwards_ordinary_methods() -> None:
    proxy, _ = _proxy()
    assert _screen(proxy, "Page.navigate") is None
    assert _screen(proxy, "Runtime.evaluate") is None
    assert _screen(proxy, "Input.dispatchMouseEvent") is None
    # Chromium's windows are the window manager's to place, so moving one is allowed.
    assert _screen(proxy, "Browser.setWindowBounds") is None


def test_losing_the_lease_refuses_the_very_next_frame_and_names_where_the_answer_lives() -> None:
    proxy, _ = _proxy(is_allowed=False)
    reason = _screen(proxy, "Input.dispatchMouseEvent")
    assert reason is not None and "not yours" in reason
    assert "ls" in reason


def test_another_agents_token_cannot_drive() -> None:
    proxy, _ = _proxy()
    assert _screen(proxy, "Page.navigate", token="someone-elses") is not None
    assert _screen(proxy, "Page.navigate", token="") is not None


@pytest.mark.parametrize("method", sorted(ALWAYS_BLOCKED))
def test_destructive_methods_are_refused_even_while_holding_the_lease(method: str) -> None:
    proxy, _ = _proxy()
    assert _screen(proxy, method) is not None
    assert "Emulation.setDeviceMetricsOverride" in ALWAYS_BLOCKED


def test_closing_the_last_page_is_refused_but_other_tabs_are_fine() -> None:
    for method in sorted(LAST_PAGE_GUARDED):
        one_page, _ = _proxy(pages=1)
        reason = _screen(one_page, method)
        assert reason is not None and "last page" in reason
        many_pages, _ = _proxy(pages=3)
        assert _screen(many_pages, method) is None


def test_setautoattach_is_forwarded_untouched() -> None:
    proxy, _ = _proxy()
    assert _screen(proxy, "Target.setAutoAttach") is None


@pytest.mark.parametrize(
    ("is_allowed", "method", "is_following"),
    [
        pytest.param(False, "Runtime.runIfWaitingForDebugger", False, id="resume-without-the-lease"),
        pytest.param(True, "Page.navigate", True, id="leased-command-follows"),
    ],
)
def test_a_resume_reaches_chromium_without_moving_the_view(is_allowed: bool, method: str, is_following: bool) -> None:
    proxy, seen = _proxy(is_allowed=is_allowed)
    proxy.note_upstream_message(
        json.dumps(
            {"method": "Target.attachedToTarget", "params": {"sessionId": "s1", "targetInfo": {"targetId": "t1"}}}
        )
    )
    forwarded: list[str | bytes] = []
    replies: list[str | bytes] = []
    proxy.forward_frame(
        json.dumps({"id": 1, "method": method, "sessionId": "s1"}), "good", replies.append, forwarded.append
    )
    assert len(forwarded) == 1 and replies == []
    if is_following:
        assert seen.activity_seen.wait(2.0)
        assert seen.activity == ["t1"]
    else:
        assert not seen.activity_seen.wait(0.4)
    proxy.cancel_follow()


def test_a_refused_frame_is_answered_with_a_cdp_error_carrying_its_id_and_session() -> None:
    proxy, _ = _proxy()
    forwarded: list[str | bytes] = []
    replies: list[str | bytes] = []
    proxy.forward_frame(
        json.dumps({"id": 7, "method": "Browser.close", "sessionId": "s9"}), "good", replies.append, forwarded.append
    )
    assert forwarded == []
    reply = json.loads(replies[0])
    assert reply["id"] == 7 and reply["sessionId"] == "s9"
    assert "not permitted" in reply["error"]["message"]


def test_discovery_rewrites_every_websocket_url_and_filters_targets_to_real_pages() -> None:
    proxy, _ = _proxy()
    version = {"Browser": "Chrome/153", "webSocketDebuggerUrl": "ws://127.0.0.1:9777/devtools/browser/abc"}
    targets = [
        {
            "type": "browser_ui",
            "url": "chrome://omnibox-popup.top-chrome/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9777/a",
        },
        {
            "type": "background_page",
            "url": "chrome-extension://x/bg.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9777/b",
        },
        {
            "type": "page",
            "url": "https://example.com",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9777/d",
            "devtoolsFrontendUrl": "/devtools/inspector.html?ws=127.0.0.1:9777/d",
        },
    ]

    def fetch(endpoint: str) -> Any:
        return version if endpoint == "/json/version" else targets

    for path in ("/browser-1/good/json/version", "/browser-1/good/json/version/"):
        response = proxy.rewrite_discovery(path, "good", fetch)
        assert response is not None
        body = json.loads(response.body)
        assert "9777" not in json.dumps(body)
        assert body["webSocketDebuggerUrl"] == "ws://127.0.0.1:2/browser-1/good"
        assert response.headers["Connection"] == "close"
    listed = proxy.rewrite_discovery("/browser-1/good/json/list/", "good", fetch)
    assert listed is not None
    pages = json.loads(listed.body)
    assert [page["type"] for page in pages] == ["page"]
    assert "9777" not in json.dumps(pages) and "devtoolsFrontendUrl" not in pages[0]
    for verb in ("new", "close", "activate"):
        refused = proxy.rewrite_discovery(f"/browser-1/good/json/{verb}", "good", fetch)
        assert refused is not None and refused.status_code == 405
    assert proxy.rewrite_discovery("/browser-1/good", "good", fetch) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/browser-1/tok", ("browser-1", "tok")),
        ("/browser-1/tok/json/version/", ("browser-1", "tok")),
        ("/browser-1/tok?x=1", ("browser-1", "tok")),
        ("/", ("", "")),
        ("/json/version", ("", "")),
    ],
)
def test_the_server_splits_name_and_token_from_the_path(path: str, expected: tuple[str, str]) -> None:
    assert split_proxy_path(path) == expected


def test_registration_is_addressable_and_removable() -> None:
    server = ProxyServer(requested_port=0)
    proxy, _ = _proxy()
    server.register(proxy)
    assert server.lookup("browser-1") is proxy
    server.unregister("browser-1")
    assert server.lookup("browser-1") is None
    assert server.attach_url("browser-1", "tok") == "http://127.0.0.1:0/browser-1/tok"


def test_the_server_relays_an_agents_frames_to_chromium_refusing_the_blocked_ones_in_place() -> None:
    with FakeChromiumServer(
        targets=[{"targetId": "t1", "type": "page", "url": "https://one.example", "title": ""}]
    ) as chromium:
        server = ProxyServer(requested_port=0)
        server.start()
        try:
            proxy, seen = _proxy(upstream_http=chromium.http_endpoint, public_base=server.public_base)
            server.register(proxy)
            with connect_websocket(f"{server.public_base}/browser-1/good", open_timeout=5) as agent:
                agent.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
                answer = json.loads(agent.recv(timeout=5))
                assert answer["id"] == 1
                assert [target["targetId"] for target in answer["result"]["targetInfos"]] == ["t1"]
                agent.send(json.dumps({"id": 2, "method": "Browser.close"}))
                refused = json.loads(agent.recv(timeout=5))
                assert refused["id"] == 2 and "not permitted" in refused["error"]["message"]
                # The refused frame never reached Chromium; the socket is still open and drives on.
                assert [frame["method"] for frame in chromium.frames] == ["Target.getTargets"]
                agent.send(json.dumps({"id": 3, "method": "Browser.getVersion"}))
                assert json.loads(agent.recv(timeout=5))["result"] == {"product": "Chrome/153"}
            assert seen.attached == 1
            assert wait_until(lambda: len(seen.activity) >= 1)
            # A frame naming no session moves nothing.
            assert seen.activity[0] is None
            with connect_websocket(f"{server.public_base}/browser-1/bad", open_timeout=5) as stranger:
                assert "not yours" in json.loads(stranger.recv(timeout=5))["error"]["message"]
        finally:
            server.stop()


def test_stopping_the_server_ends_the_live_relays_so_their_threads_do_not_outlive_it() -> None:
    with FakeChromiumServer(
        targets=[{"targetId": "t1", "type": "page", "url": "https://one.example", "title": ""}]
    ) as chromium:
        server = ProxyServer(requested_port=0)
        server.start()
        proxy, _ = _proxy(upstream_http=chromium.http_endpoint, public_base=server.public_base)
        server.register(proxy)
        with connect_websocket(f"{server.public_base}/browser-1/good", open_timeout=5) as agent:
            agent.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            assert json.loads(agent.recv(timeout=5))["id"] == 1
            server.stop()
            with pytest.raises(ConnectionClosed):
                agent.recv(timeout=5)
        assert wait_until(lambda: not any(thread.name == "screen-cdp-pump" for thread in threading.enumerate()))


def test_the_server_answers_discovery_over_http_and_an_unknown_browser_with_404() -> None:
    with FakeChromiumServer(
        targets=[{"targetId": "t1", "type": "page", "url": "https://one.example", "title": ""}]
    ) as chromium:
        server = ProxyServer(requested_port=0)
        server.start()
        try:
            proxy, _ = _proxy(upstream_http=chromium.http_endpoint, public_base=server.public_base)
            server.register(proxy)
            base = f"http://127.0.0.1:{server.port}"
            version = httpx.get(f"{base}/browser-1/good/json/version/", timeout=5)
            assert version.status_code == 200
            assert version.json()["webSocketDebuggerUrl"] == f"{server.public_base}/browser-1/good"
            assert version.headers["connection"] == "close"
            assert httpx.get(f"{base}/nobody/tok/json/version", timeout=5).status_code == 404
        finally:
            server.stop()
