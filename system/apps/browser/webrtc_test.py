import asyncio
import base64
import io
import json
import queue
from typing import Any

import av
import pytest
from aiortc import RTCConfiguration
from aiortc import RTCPeerConnection
from browser import session as bsession
from browser import webrtc


def _make_jpeg_b64(width: int = 64, height: int = 48) -> str:
    """A tiny valid JPEG (base64), shaped like a CDP screencast frame."""
    frame = av.VideoFrame(width, height, "rgb24")
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mjpeg") as container:
        stream = container.add_stream("mjpeg", rate=1)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuvj420p"
        for packet in stream.encode(frame):
            container.mux(packet)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture(autouse=True)
def _clean_ice_config(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with no TURN config and an empty minted-credentials cache."""
    for key in (
        "BROWSER_WEBRTC_DISABLED",
        "BROWSER_TURN_ICE_ENDPOINT",
        "BROWSER_TURN_ICE_TOKEN",
        "BROWSER_TURN_URLS",
        "BROWSER_TURN_USERNAME",
        "BROWSER_TURN_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    webrtc._ice_cache.update({"expires_at": 0.0, "servers": None})
    yield
    webrtc._ice_cache.update({"expires_at": 0.0, "servers": None})


# --- config -------------------------------------------------------------------


def test_webrtc_enabled_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert webrtc.webrtc_enabled() is True
    monkeypatch.setenv("BROWSER_WEBRTC_DISABLED", "1")
    assert webrtc.webrtc_enabled() is False


def test_client_ice_servers_is_stun_only_without_turn_config() -> None:
    servers = asyncio.run(webrtc.client_ice_servers())
    assert len(servers) == 1
    assert servers[0]["urls"][0].startswith("stun:")


def test_client_ice_servers_uses_static_env_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_TURN_USERNAME", "envuser")
    monkeypatch.setenv("BROWSER_TURN_PASSWORD", "envpass")
    monkeypatch.setenv("BROWSER_TURN_URLS", "turns:turn.example.com:5349, turn:turn.example.com:3478")
    servers = asyncio.run(webrtc.client_ice_servers())
    assert servers[0]["urls"][0].startswith("stun:")
    turn = servers[1]
    assert turn["username"] == "envuser"
    assert turn["credential"] == "envpass"
    assert turn["urls"] == ["turns:turn.example.com:5349", "turn:turn.example.com:3478"]


def test_client_ice_servers_prefers_minted_credentials_and_caches_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minted = [
        {"urls": ["stun:stun.cloudflare.com:3478"]},
        {"urls": ["turn:turn.cloudflare.com:3478?transport=udp"], "username": "u", "credential": "c"},
    ]
    calls: list[str] = []

    def fake_fetch(endpoint: str) -> list[dict[str, Any]]:
        calls.append(endpoint)
        return minted

    monkeypatch.setenv("BROWSER_TURN_ICE_ENDPOINT", "https://mngr.example/turn-ice")
    monkeypatch.setattr(webrtc, "_fetch_minted_ice_servers", fake_fetch)

    assert asyncio.run(webrtc.client_ice_servers()) == minted
    assert asyncio.run(webrtc.client_ice_servers()) == minted  # served from the cache
    assert calls == ["https://mngr.example/turn-ice"]


def test_client_ice_servers_falls_back_when_the_mint_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_fetch(endpoint: str) -> list[dict[str, Any]]:
        raise OSError("endpoint down")

    monkeypatch.setenv("BROWSER_TURN_ICE_ENDPOINT", "https://mngr.example/turn-ice")
    monkeypatch.setenv("BROWSER_TURN_USERNAME", "envuser")
    monkeypatch.setenv("BROWSER_TURN_PASSWORD", "envpass")
    monkeypatch.setenv("BROWSER_TURN_URLS", "turn:turn.example.com:3478")
    monkeypatch.setattr(webrtc, "_fetch_minted_ice_servers", failing_fetch)

    servers = asyncio.run(webrtc.client_ice_servers())
    assert servers[1]["urls"] == ["turn:turn.example.com:3478"]  # static env still works
    # The failure is cached so a down endpoint isn't hammered per viewer connect.
    assert webrtc._ice_cache["servers"] is None
    assert webrtc._ice_cache["expires_at"] > 0.0


def test_normalize_ice_servers_accepts_cloudflare_and_bare_shapes() -> None:
    cloudflare_shape = {
        "iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478"]},
            {"urls": ["turn:turn.cloudflare.com:3478"], "username": "u", "credential": "c"},
        ]
    }
    normalized = webrtc._normalize_ice_servers(cloudflare_shape)
    assert normalized is not None and normalized[1]["username"] == "u"
    bare = [{"urls": ["stun:stun.example.com"]}]
    assert webrtc._normalize_ice_servers(bare) == bare
    assert webrtc._normalize_ice_servers({"nope": True}) is None
    assert webrtc._normalize_ice_servers([{"no_urls": True}]) is None
    assert webrtc._normalize_ice_servers([]) is None


# --- the screencast track -------------------------------------------------------


def test_screencast_track_streams_placeholder_then_real_frames() -> None:
    frames: list[str | None] = [None]

    async def go() -> None:
        track = webrtc.ScreencastVideoTrack(lambda: frames[0])
        # No screencast frame yet: a black placeholder keeps the stream alive.
        first = await track.recv()
        assert (first.width, first.height) == (webrtc._FALLBACK_WIDTH, webrtc._FALLBACK_HEIGHT)
        # A real frame lands: the next tick decodes and streams it.
        frames[0] = _make_jpeg_b64(64, 48)
        second = await track.recv()
        assert (second.width, second.height) == (64, 48)
        # Unchanged frame: the cached decode is re-sent (no re-decode) with a new pts.
        third = await track.recv()
        assert third is second
        assert third.pts is not None and second.pts is not None

    asyncio.run(go())


def test_screencast_track_survives_undecodable_frames() -> None:
    async def go() -> None:
        track = webrtc.ScreencastVideoTrack(lambda: base64.b64encode(b"not a jpeg").decode("ascii"))
        frame = await track.recv()  # falls back to the placeholder instead of raising
        assert (frame.width, frame.height) == (webrtc._FALLBACK_WIDTH, webrtc._FALLBACK_HEIGHT)

    asyncio.run(go())


# --- offer/answer + session signaling -------------------------------------------


def _running_browser(browser_id: str = "b1") -> bsession.LiveBrowser:
    browser = bsession.LiveBrowser(browser_id=browser_id)
    browser._lifecycle = "running"
    return browser


def _pop_json(cast_queue: "queue.Queue[str | None]") -> dict[str, Any]:
    payload = cast_queue.get_nowait()
    assert payload is not None
    return json.loads(payload)


def test_handle_webrtc_config_replies_to_the_one_client() -> None:
    browser = _running_browser()

    async def go() -> None:
        asking = await browser.register_cast_queue()
        other = await browser.register_cast_queue()
        while not asking.empty():  # drain the register-time seed
            asking.get_nowait()
        while not other.empty():
            other.get_nowait()
        await browser.handle_webrtc_message({"type": "webrtc_config"}, asking)
        reply = _pop_json(asking)
        assert reply["type"] == "webrtc_config"
        assert reply["enabled"] is True
        assert reply["ice_servers"][0]["urls"][0].startswith("stun:")
        assert other.empty()  # a per-client reply, not a broadcast

    asyncio.run(go())


def test_handle_webrtc_config_reports_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_WEBRTC_DISABLED", "1")
    browser = _running_browser()

    async def go() -> None:
        asking = await browser.register_cast_queue()
        while not asking.empty():
            asking.get_nowait()
        await browser.handle_webrtc_message({"type": "webrtc_config"}, asking)
        reply = _pop_json(asking)
        assert reply["enabled"] is False
        assert reply["ice_servers"] == []

    asyncio.run(go())


def test_webrtc_offer_answer_roundtrip_and_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real (in-process) viewer offer gets a complete answer, and unregistering the
    cast queue closes the peer. Uses aiortc as the 'viewer' so the SDP is genuine.
    STUN is skipped so ICE gathering is host-candidates-only (instant, no network)."""
    monkeypatch.setenv("BROWSER_WEBRTC_NO_STUN", "1")
    browser = _running_browser()
    browser._latest_frame = _make_jpeg_b64()

    async def go() -> None:
        cast_queue = await browser.register_cast_queue()
        while not cast_queue.empty():
            cast_queue.get_nowait()
        viewer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        viewer.addTransceiver("video", direction="recvonly")
        await viewer.setLocalDescription(await viewer.createOffer())
        await browser.handle_webrtc_message(
            {"type": "webrtc_offer", "sdp": viewer.localDescription.sdp}, cast_queue
        )
        reply = _pop_json(cast_queue)
        assert reply["type"] == "webrtc_answer"
        assert "m=video" in reply["sdp"]
        assert cast_queue in browser._rtc_peers
        await browser.unregister_cast_queue(cast_queue)
        assert cast_queue not in browser._rtc_peers
        assert cast_queue not in browser._rtc_suppressed
        await viewer.close()

    asyncio.run(go())


def test_webrtc_bad_offer_replies_error_and_leaves_no_peer() -> None:
    browser = _running_browser()

    async def go() -> None:
        cast_queue = await browser.register_cast_queue()
        while not cast_queue.empty():
            cast_queue.get_nowait()
        await browser.handle_webrtc_message({"type": "webrtc_offer", "sdp": "not an sdp"}, cast_queue)
        reply = _pop_json(cast_queue)
        assert reply["type"] == "webrtc_error"
        assert cast_queue not in browser._rtc_peers

    asyncio.run(go())


def test_broadcast_suppresses_frames_only_for_rtc_connected_clients() -> None:
    browser = _running_browser()

    async def go() -> None:
        rtc_client = await browser.register_cast_queue()
        ws_client = await browser.register_cast_queue()
        while not rtc_client.empty():
            rtc_client.get_nowait()
        while not ws_client.empty():
            ws_client.get_nowait()
        browser._set_rtc_suppressed(rtc_client, True)

        browser._broadcast({"type": "frame", "data": "abc"})
        browser._broadcast({"type": "ping"})
        # The WebRTC-connected client skips the JPEG frame but still gets control traffic.
        assert _pop_json(rtc_client)["type"] == "ping"
        assert rtc_client.empty()
        assert _pop_json(ws_client)["type"] == "frame"
        assert _pop_json(ws_client)["type"] == "ping"

        # The transport dropping lifts suppression AND replays the latest frame so a
        # static page repaints immediately.
        browser._latest_frame = "latest"
        browser._set_rtc_suppressed(rtc_client, False)
        replay = _pop_json(rtc_client)
        assert replay == {"type": "frame", "data": "latest"}

    asyncio.run(go())
