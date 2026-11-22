"""The fleet's CDP client against a stand-in Chromium over real sockets, and its pure helpers."""

import pytest

from screen.cdp_client import CdpClient
from screen.cdp_client import WebSocketCdpConnector
from screen.cdp_client import dict_at
from screen.cdp_client import is_real_page
from screen.cdp_client import order_targets
from screen.errors import CdpError
from testing import FakeChromiumServer
from testing import wait_until

_TAB_ONE = {"targetId": "t1", "type": "page", "url": "https://one.example", "title": "One"}
_TAB_TWO = {"targetId": "t2", "type": "page", "url": "https://two.example", "title": "Two"}


def test_only_real_pages_count_as_tabs() -> None:
    assert is_real_page(_TAB_ONE) is True
    assert is_real_page({"type": "background_page", "url": "chrome-extension://x/bg.html"}) is False
    assert is_real_page({"type": "page", "url": "chrome://newtab/"}) is False
    assert is_real_page({"type": "page", "url": "devtools://devtools/x"}) is False
    assert is_real_page({"type": "service_worker", "url": "https://x/sw.js"}) is False


def test_targets_are_ordered_by_creation_with_unknown_ones_last() -> None:
    ordered = order_targets(
        [_TAB_TWO, {"targetId": "t9", "type": "page", "url": "https://nine"}, _TAB_ONE], ["t1", "t2"]
    )
    assert [target.target_id for target in ordered] == ["t1", "t2", "t9"]
    assert ordered[0].title == "One"
    assert dict_at({"a": {"b": 1}}, "a") == {"b": 1}
    assert dict_at({"a": "not a dict"}, "a") == {}


def test_the_client_lists_tabs_in_creation_order_and_tracks_new_and_closed_ones() -> None:
    with FakeChromiumServer(targets=[dict(_TAB_ONE), dict(_TAB_TWO)]) as chromium:
        client = WebSocketCdpConnector().connect(chromium.http_endpoint)
        try:
            assert [target.target_id for target in client.page_targets()] == ["t1", "t2"]
            created = client.create_target("https://three.example")
            assert created == "t3"
            assert wait_until(lambda: [target.target_id for target in client.page_targets()] == ["t1", "t2", "t3"])
            client.close_target("t2")
            assert wait_until(lambda: [target.target_id for target in client.page_targets()] == ["t1", "t3"])
            client.activate("t3")
            assert client.is_alive()
            assert client.ping() is True
        finally:
            client.close()
        assert client.is_alive() is False
        methods = [frame["method"] for frame in chromium.frames]
        assert methods[0] == "Target.setDiscoverTargets"
        assert "Target.activateTarget" in methods


def test_is_shown_attaches_to_the_tab_asks_the_page_and_detaches_again() -> None:
    with FakeChromiumServer(targets=[dict(_TAB_ONE), dict(_TAB_TWO)]) as chromium:
        client = WebSocketCdpConnector().connect(chromium.http_endpoint)
        try:
            assert client.is_shown("t1") is True
            assert client.is_shown("t2") is False
        finally:
            client.close()
        sequence = [
            (frame["method"], frame.get("sessionId"))
            for frame in chromium.frames
            if frame["method"] != "Target.setDiscoverTargets"
        ]
        assert sequence == [
            ("Target.attachToTarget", None),
            ("Runtime.evaluate", "session-t1"),
            ("Target.detachFromTarget", None),
            ("Target.attachToTarget", None),
            ("Runtime.evaluate", "session-t2"),
            ("Target.detachFromTarget", None),
        ]


def test_a_call_chromium_never_answers_times_out_and_a_refusal_is_an_error() -> None:
    with FakeChromiumServer(targets=[dict(_TAB_ONE)], unanswered_methods={"Target.activateTarget"}) as chromium:
        client = CdpClient(http_endpoint=chromium.http_endpoint)
        client.connect()
        try:
            with pytest.raises(CdpError, match="timed out"):
                client.send("Target.activateTarget", {"targetId": "t1"}, timeout_seconds=0.2)
            with pytest.raises(CdpError, match="never reached"):
                client.send("Browser.close")
        finally:
            client.close()


def test_connecting_to_nothing_is_a_cdp_error() -> None:
    with pytest.raises(CdpError):
        WebSocketCdpConnector().connect("http://127.0.0.1:1")
