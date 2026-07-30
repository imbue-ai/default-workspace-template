"""WebRTC live-view transport: the CDP screencast re-encoded as real video.

The JPEG-over-WebSocket cast stream (see session.py) tunnels every frame as a
base64 JSON message through the system_interface WS proxy -- simple, but heavy
(whole re-sent frames, no congestion control) and bound by the proxy's 1 MiB
per-message cap. This module adds a parallel WebRTC path: the same screencast
frames are decoded once and re-encoded as a real video stream (VP8/H264 delta
frames with built-in congestion control) over an RTCPeerConnection straight
between the viewer and this service, bypassing the HTTP proxy entirely. The
JPEG stream stays as the always-works fallback; the viewer switches to it
whenever the peer connection can't be established (see assets/index.html).

Signaling rides the existing per-browser cast WebSocket (``webrtc_config`` /
``webrtc_offer`` / ``webrtc_answer`` messages, routed by session.py's
``handle_webrtc_message``); this module owns all the aiortc objects.

ICE topology: the SERVER answers with plain host candidates (aiortc's default
STUN config, no TURN) -- a workspace with a reachable address needs nothing
more. When the workspace sits behind an HTTP-only ingress (the cloudflared
tunnel), the VIEWER must relay its media through a TURN server, so the client's
ICE server list is minted per session by a trusted endpoint
(``BROWSER_TURN_ICE_ENDPOINT`` -- the mngr cloud service that holds the
Cloudflare TURN key and tags each credential with the user for billing) and
handed to the viewer over signaling. A static env fallback
(``BROWSER_TURN_URLS``/``USERNAME``/``PASSWORD``) covers dev and bring-your-own
TURN; with neither configured the viewer gets STUN only, which still covers
every directly-reachable deployment (and the JPEG stream covers the rest).
"""

import asyncio
import base64
import json
import os
import time
import urllib.request
from collections.abc import Callable
from typing import Any

import av
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import VideoStreamTrack
from loguru import logger

# Errors expected from signaling with an arbitrary remote SDP / a dying transport.
SIGNALING_ERRORS = (ValueError, OSError, RuntimeError, ConnectionError)

# Always-present ICE entry: free, no credentials, covers every deployment where
# the viewer can reach the workspace's host candidates directly.
_STUN_SERVER: dict[str, Any] = {"urls": ["stun:stun.l.google.com:19302"]}

# How long a minted ICE server list is reused before re-asking the endpoint. The
# endpoint should mint credentials with a TTL comfortably above this (e.g. 4h) so
# a list is never handed out close to its expiry. A failed fetch is also cached
# (briefly) so a down endpoint isn't hammered on every viewer connect.
_ICE_CACHE_SECONDS = float(os.environ.get("BROWSER_TURN_ICE_CACHE_SECONDS", "1800"))
_ICE_FAILURE_CACHE_SECONDS = 60.0
_ICE_FETCH_TIMEOUT_SECONDS = 10.0

# Minted-list cache (single-element holder; only ever touched from the bridge
# loop thread, so no lock is needed). ``servers`` is None when the last fetch
# failed or no fetch has happened yet.
_ice_cache: dict[str, Any] = {"expires_at": 0.0, "servers": None}

# Size of the black placeholder frame streamed until the first screencast frame
# lands (a fresh browser that has not repainted yet).
_FALLBACK_WIDTH = 640
_FALLBACK_HEIGHT = 480


def webrtc_enabled() -> bool:
    """Kill switch: BROWSER_WEBRTC_DISABLED=1 turns the WebRTC path off entirely
    (the viewer then stays on the JPEG-over-WebSocket stream)."""
    return os.environ.get("BROWSER_WEBRTC_DISABLED", "").strip().lower() not in ("1", "true", "yes", "on")


def _static_turn_server() -> dict[str, Any] | None:
    """A TURN entry from static env -- dev and bring-your-own-TURN deployments."""
    urls_raw = os.environ.get("BROWSER_TURN_URLS", "").strip()
    username = os.environ.get("BROWSER_TURN_USERNAME")
    password = os.environ.get("BROWSER_TURN_PASSWORD")
    if not (urls_raw and username and password):
        return None
    urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
    return {"urls": urls, "username": username, "credential": password}


def _normalize_ice_servers(payload: Any) -> list[dict[str, Any]] | None:
    """Extract an RTCPeerConnection-ready ICE server list from an endpoint response.

    Accepts the Cloudflare credential-mint shape (``{"iceServers": [...]}`` --
    what the mngr endpoint proxies through) or a bare list. Returns None when the
    payload has neither."""
    servers = payload.get("iceServers") if isinstance(payload, dict) else payload
    if not isinstance(servers, list) or not servers:
        return None
    normalized: list[dict[str, Any]] = []
    for server in servers:
        if not isinstance(server, dict) or "urls" not in server:
            return None
        entry: dict[str, Any] = {"urls": server["urls"]}
        if server.get("username"):
            entry["username"] = server["username"]
        if server.get("credential"):
            entry["credential"] = server["credential"]
        normalized.append(entry)
    return normalized


def _fetch_minted_ice_servers(endpoint: str) -> list[dict[str, Any]] | None:
    """One blocking GET to the trusted minting endpoint (run via a worker thread).

    ``BROWSER_TURN_ICE_TOKEN`` (optional) rides as a bearer header for endpoints
    that want explicit auth on top of whatever channel carries the request."""
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    token = os.environ.get("BROWSER_TURN_ICE_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=_ICE_FETCH_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    return _normalize_ice_servers(payload)


async def client_ice_servers() -> list[dict[str, Any]]:
    """The ICE server list for the VIEWER's RTCPeerConnection, as JSON-ready dicts.

    Sourced in priority order: the minting endpoint (per-session Cloudflare TURN
    credentials, cached here until ``_ICE_CACHE_SECONDS``), static env TURN, else
    STUN only. Async because the endpoint fetch is real network I/O (pushed to a
    worker thread so the bridge loop never blocks on it).
    """
    endpoint = os.environ.get("BROWSER_TURN_ICE_ENDPOINT", "").strip()
    if endpoint:
        now = time.monotonic()
        if now >= _ice_cache["expires_at"]:
            try:
                servers = await asyncio.to_thread(_fetch_minted_ice_servers, endpoint)
            except (OSError, ValueError) as e:
                logger.warning("TURN credential mint from {} failed ({})", endpoint, e)
                servers = None
            if servers is None:
                _ice_cache["expires_at"] = now + _ICE_FAILURE_CACHE_SECONDS
                _ice_cache["servers"] = None
            else:
                _ice_cache["expires_at"] = now + _ICE_CACHE_SECONDS
                _ice_cache["servers"] = servers
        if _ice_cache["servers"] is not None:
            return _ice_cache["servers"]
    static_turn = _static_turn_server()
    if static_turn is not None:
        return [_STUN_SERVER, static_turn]
    return [_STUN_SERVER]


def _server_ice_servers() -> list[RTCIceServer]:
    """ICE servers for the SERVER side of each peer connection: STUN only (server
    reflexive candidates matter when the workspace has NAT-but-open outbound UDP;
    the TURN relaying, when needed, is the viewer's job). BROWSER_WEBRTC_NO_STUN=1
    skips it for offline dev/tests, where the STUN query would only stall ICE
    gathering until its timeout."""
    if os.environ.get("BROWSER_WEBRTC_NO_STUN", "").strip().lower() in ("1", "true", "yes", "on"):
        return []
    return [RTCIceServer(urls=_STUN_SERVER["urls"])]


class ScreencastVideoTrack(VideoStreamTrack):
    """A video track fed by the session's latest CDP screencast JPEG.

    ``get_frame`` returns the session's ``_latest_frame`` (base64 JPEG, or None
    before the first repaint). The base class paces ``recv`` at its fixed video
    clock; each tick decodes the JPEG only when it changed since the last tick
    (an unchanged static page re-sends the previously decoded frame, which the
    encoder turns into cheap skip frames). Until the first real frame arrives a
    black placeholder is streamed so the connection comes up immediately.
    """

    def __init__(self, get_frame: Callable[[], str | None]) -> None:
        super().__init__()
        self._get_frame = get_frame
        self._decoder = av.CodecContext.create("mjpeg", "r")
        self._last_b64: str | None = None
        self._last_frame: av.VideoFrame | None = None

    def _decode_if_changed(self) -> None:
        b64 = self._get_frame()
        # Comparing to the previous string is cheap: an unchanged _latest_frame is
        # the SAME str object (identity short-circuits) and a new frame differs early.
        if b64 is None or b64 == self._last_b64:
            return
        try:
            frames = self._decoder.decode(av.Packet(base64.b64decode(b64)))
        except (av.FFmpegError, ValueError) as e:
            logger.debug("webrtc screencast frame decode ignored ({})", e)
            return
        if frames:
            self._last_frame = frames[-1]
            self._last_b64 = b64

    def _placeholder_frame(self) -> "av.VideoFrame":
        frame = av.VideoFrame(_FALLBACK_WIDTH, _FALLBACK_HEIGHT, "rgb24")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        return frame

    async def recv(self) -> "av.VideoFrame":
        pts, time_base = await self.next_timestamp()
        self._decode_if_changed()
        frame = self._last_frame
        if frame is None:
            frame = self._placeholder_frame()
            self._last_frame = frame
        frame.pts = pts
        frame.time_base = time_base
        return frame


class CastPeer:
    """One viewer's RTCPeerConnection, answering its offer with the screencast track.

    ``on_connected`` / ``on_gone`` report the media transport coming up / going
    away, so the session can stop (resume) fanning JPEG frames to that viewer's
    cast queue while WebRTC carries (stops carrying) the pixels. Both fire on the
    aiortc event loop -- the same single loop the session runs on -- and must be
    idempotent (``on_gone`` also fires for our own :meth:`close`).
    """

    def __init__(
        self,
        get_frame: Callable[[], str | None],
        on_connected: Callable[[], None],
        on_gone: Callable[[], None],
    ) -> None:
        self._pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=_server_ice_servers()))
        self._pc.addTrack(ScreencastVideoTrack(get_frame))

        @self._pc.on("connectionstatechange")
        async def _on_state() -> None:
            state = self._pc.connectionState
            if state == "connected":
                on_connected()
            elif state in ("failed", "closed", "disconnected"):
                on_gone()

    async def answer(self, offer_sdp: str) -> str:
        """Accept the viewer's offer and return the complete (non-trickle) answer SDP.

        aiortc gathers every ICE candidate inside ``setLocalDescription``, so the
        returned SDP is self-contained -- no separate candidate signaling."""
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        return self._pc.localDescription.sdp

    async def close(self) -> None:
        await self._pc.close()
