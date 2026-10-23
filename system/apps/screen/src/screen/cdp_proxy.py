"""The agent's gated CDP endpoint (spec sections 5.1 and 16.1).

Agents do not get Chromium's real debug port; they get ``ws://127.0.0.1:8083/<name>/<token>``. Every frame an agent
sends passes through the proxy, which is where the Screen's control lease is enforced per frame.

Two points were settled by measurement against ``@playwright/cli`` and are load-bearing: refuse frames and never
close the socket (a playwright-cli session is poisoned the moment its socket drops; a refused frame leaves it
healthy and it drives again as soon as the lease comes back), and serve discovery on both the bare and the
trailing-slash paths (Playwright fetches ``/json/version/``).

This is a guardrail, not a security boundary: the agent has a shell and can read ``DevToolsActivePort`` out of
the profile directory, so the block list is sized for what the CLI sends rather than for a hostile client.
"""

import json
import threading
from collections.abc import Callable
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from websockets.datastructures import Headers
from websockets.exceptions import WebSocketException
from websockets.http11 import Request
from websockets.http11 import Response
from websockets.sync.client import connect as connect_websocket
from websockets.sync.server import Server
from websockets.sync.server import ServerConnection
from websockets.sync.server import serve

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.cdp_client import browser_websocket_url
from screen.cdp_client import dict_at
from screen.errors import CdpError
from screen.primitives import CDP_PROXY_HOST
from screen.primitives import CDP_PROXY_PORT

# Methods refused even while the agent legitimately holds the lease: deliberately short and enumerated, since a
# general-purpose CDP filter breaks the CLI in ways nobody can predict. ``Browser.setWindowBounds`` is allowed:
# Chromium's windows are ordinary X windows that nothing pins (spec section 16.1).
ALWAYS_BLOCKED: Final[frozenset[str]] = frozenset(
    {
        # Kills the browser the human may be holding, behind the fleet's back.
        "Browser.close",
        # An incognito context has no profile persistence and its targets are invisible to the fleet's tab list.
        "Target.createBrowserContext",
        "Target.disposeBrowserContext",
        # Overrides the page viewport independently of the window, which nothing would ever undo.
        "Emulation.setDeviceMetricsOverride",
        # Redirects downloads to an arbitrary path inside the workspace.
        "Browser.setDownloadBehavior",
        "Page.setDownloadBehavior",
    }
)

# Refused only when it would close the browser's last remaining page.
LAST_PAGE_GUARDED: Final[frozenset[str]] = frozenset({"Target.closeTarget", "Page.close"})

# Forwarded even from a client that no longer holds the lease. Playwright auto-attaches with
# waitForDebuggerOnStart, so Chromium holds every new tab or popup until that client resumes it; refusing the
# resume after a handoff would freeze the page that opened it (they share a renderer). A resume is not the agent
# acting on a tab, so it does not move the view either.
ALWAYS_FORWARDED: Final[frozenset[str]] = frozenset({"Runtime.runIfWaitingForDebugger"})

# CDP is far chattier than one call per verb, so "agent acts, the view follows" is coalesced per frame burst.
FOLLOW_DEBOUNCE_SECONDS: Final[float] = 0.25

LEASE_LOST_MESSAGE: Final[str] = (
    "browser is not yours right now -- the human took control, or your lease expired. "
    "Run `agentic-browser-fleet ls` to see who holds it."
)
NOT_YOURS_MESSAGE: Final[str] = "this browser is not yours -- run `agentic-browser-fleet ls`"

_DISCOVERY_TIMEOUT_SECONDS: Final[float] = 5.0
_UPSTREAM_OPEN_TIMEOUT_SECONDS: Final[float] = 10.0
_POLL_SECONDS: Final[float] = 0.5
_PUMP_JOIN_SECONDS: Final[float] = 5.0
_SERVER_JOIN_SECONDS: Final[float] = 5.0
HTTP_METHOD_NOT_ALLOWED: Final[int] = 405
HTTP_NOT_FOUND: Final[int] = 404
HTTP_BAD_GATEWAY: Final[int] = 502
# WebSocket close codes: policy violation (an unknown browser) and try again later (an upstream that is down).
WS_CLOSE_POLICY_VIOLATION: Final[int] = 1008
WS_CLOSE_TRY_AGAIN_LATER: Final[int] = 1013


@pure
def cdp_error_frame(frame_id: Any, session_id: str | None, message: str, code: int = -32000) -> str:
    reply: dict[str, Any] = {"id": frame_id, "error": {"code": code, "message": message}}
    if session_id:
        reply["sessionId"] = session_id
    return json.dumps(reply)


@pure
def split_proxy_path(path: str) -> tuple[str, str]:
    """The browser name and token a proxy path carries: ``/<name>/<token>[/json/...][?...]``."""
    parts = [part for part in path.split("?")[0].split("/") if part]
    if len(parts) >= 2 and parts[0] != "json":
        return parts[0], parts[1]
    return "", ""


@pure
def discovery_response(payload: Any, status: int = 200) -> Response:
    """A discovery answer, and it must close the connection: without ``Connection: close`` the client sends the
    WebSocket upgrade on a connection this server is done with, the upgrade hangs up and the CLI exits."""
    body = json.dumps(payload).encode()
    headers = Headers({"Content-Type": "application/json", "Content-Length": str(len(body)), "Connection": "close"})
    return Response(status, "OK" if status == 200 else "Error", headers, body)


class BrowserProxy(MutableModel):
    """Proxies one browser: discovery rewriting, the per-frame gate, and the view following the agent."""

    model_config = {"arbitrary_types_allowed": True}

    name: str = Field(frozen=True, description="The browser the proxy serves")
    upstream_http: str = Field(frozen=True, description="Chromium's debug HTTP origin")
    public_base: str = Field(frozen=True, description="The proxy's own WebSocket origin, ``ws://host:port``")
    is_allowed: Callable[[str], bool] = Field(frozen=True, description="Whether a token may drive right now")
    on_attach: Callable[[], None] = Field(frozen=True, description="The first attach by an agent")
    on_activity: Callable[[str | None], None] = Field(
        frozen=True, description="The agent acted on a target (or none it named): the view follows it"
    )
    page_count: Callable[[], int] = Field(frozen=True, description="For the last-page guard")

    _sessions: dict[str, str] = PrivateAttr(default_factory=dict)
    _follow_timer: threading.Timer | None = PrivateAttr(default=None)
    _is_attached_once: bool = PrivateAttr(default=False)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def fetch_discovery(self, endpoint: str) -> Any:
        try:
            response = httpx.get(f"{self.upstream_http.rstrip('/')}{endpoint}", timeout=_DISCOVERY_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise CdpError(f"Chromium's {endpoint} could not be read: {e}") from e

    def rewrite_discovery(self, path: str, token: str, fetch: Callable[[str], Any] | None = None) -> Response | None:
        """Serve ``/json/version[/]`` and ``/json/list[/]`` with the proxy's URLs substituted, refuse the legacy
        mutating HTTP endpoints, and answer None for any other path (the WebSocket handshake proceeds)."""
        fetch_json = fetch if fetch is not None else self.fetch_discovery
        bare = path.split("?")[0].rstrip("/")
        public_url = f"{self.public_base}/{self.name}/{token}"
        if bare.endswith("/json/version"):
            payload = fetch_json("/json/version")
            if isinstance(payload, dict):
                payload["webSocketDebuggerUrl"] = public_url
            return discovery_response(payload)
        if bare.endswith("/json/list"):
            raw = fetch_json("/json/list")
            listed = raw if isinstance(raw, list) else []
            targets = [target for target in listed if isinstance(target, dict) and target.get("type") == "page"]
            for target in targets:
                target.pop("devtoolsFrontendUrl", None)
                target["webSocketDebuggerUrl"] = public_url
            return discovery_response(targets)
        if any(bare.endswith(f"/json/{verb}") for verb in ("new", "close", "activate")):
            return discovery_response(
                {"error": "not available through the fleet proxy; use CDP"}, status=HTTP_METHOD_NOT_ALLOWED
            )
        return None

    def screen(self, frame: dict[str, Any], token: str) -> str | None:
        """The reason to refuse this frame, or None to forward it."""
        method = frame.get("method", "")
        if method in ALWAYS_FORWARDED:
            return None
        if not self.is_allowed(token):
            return LEASE_LOST_MESSAGE
        if method in ALWAYS_BLOCKED:
            return f"{method} is not permitted through the fleet proxy"
        if method in LAST_PAGE_GUARDED:
            is_last_page = self.page_count() <= 1
            # The page count is a real CDP round trip, so the lease may have moved while it ran.
            if not self.is_allowed(token):
                return LEASE_LOST_MESSAGE
            if is_last_page:
                return (
                    f"{method} would close the browser's last page. Use `agentic-browser-fleet close` to end the "
                    "browser instead."
                )
        return None

    def note_upstream_message(self, raw: str | bytes) -> None:
        """Keep the session-to-target map from Chromium's attach and detach events."""
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        params = dict_at(message, "params")
        if message.get("method") == "Target.attachedToTarget":
            session_id = params.get("sessionId")
            target_id = dict_at(params, "targetInfo").get("targetId")
            if isinstance(session_id, str) and isinstance(target_id, str) and session_id and target_id:
                with self._lock:
                    self._sessions[session_id] = target_id
        elif message.get("method") == "Target.detachedFromTarget":
            with self._lock:
                self._sessions.pop(str(params.get("sessionId", "")), None)
        else:
            pass

    def schedule_follow(self, frame: dict[str, Any]) -> bool:
        """After a forwarded command, have the view follow the target it addressed (debounced); answers whether a
        follow was scheduled (a resume never moves the view)."""
        method = frame.get("method")
        if not method or method in ALWAYS_FORWARDED:
            return False
        with self._lock:
            target_id = self._sessions.get(str(frame.get("sessionId", "")))
            if self._follow_timer is not None:
                self._follow_timer.cancel()
            timer = threading.Timer(FOLLOW_DEBOUNCE_SECONDS, self.on_activity, args=(target_id,))
            timer.daemon = True
            self._follow_timer = timer
        timer.start()
        return True

    def cancel_follow(self) -> None:
        with self._lock:
            if self._follow_timer is not None:
                self._follow_timer.cancel()
                self._follow_timer = None

    def pump(self, client: ServerConnection, token: str) -> None:
        """Bridge one agent socket to Chromium, gating every frame the agent sends: this thread carries agent to
        Chromium, a second thread the other way, and either side closing ends both."""
        if not self.is_allowed(token):
            # Refused before an upstream socket is opened: the frame tells the agent where the answer lives.
            client.send(cdp_error_frame(None, None, NOT_YOURS_MESSAGE))
            return
        try:
            upstream = connect_websocket(
                browser_websocket_url(self.upstream_http), max_size=None, open_timeout=_UPSTREAM_OPEN_TIMEOUT_SECONDS
            )
        except (CdpError, WebSocketException, OSError) as e:
            logger.debug("Chromium's browser socket could not be opened for the proxy ({})", e)
            client.close(WS_CLOSE_TRY_AGAIN_LATER)
            return
        if not self._is_attached_once:
            self._is_attached_once = True
            self.on_attach()
        stop = threading.Event()

        def pump_upstream_to_client() -> None:
            try:
                while not stop.is_set():
                    try:
                        raw = upstream.recv(timeout=_POLL_SECONDS)
                    except TimeoutError:
                        continue
                    self.note_upstream_message(raw)
                    client.send(raw)
            except (WebSocketException, OSError) as e:
                logger.debug("The CDP relay's Chromium-to-agent side ended ({!r})", e)
            finally:
                stop.set()

        pump = threading.Thread(target=pump_upstream_to_client, name="screen-cdp-pump", daemon=True)
        pump.start()
        try:
            while not stop.is_set():
                try:
                    raw = client.recv(timeout=_POLL_SECONDS)
                except TimeoutError:
                    continue
                self.forward_frame(raw, token, client.send, upstream.send)
        except (WebSocketException, OSError) as e:
            logger.debug("The CDP relay's agent-to-Chromium side ended ({!r})", e)
        finally:
            stop.set()
            try:
                upstream.close()
            except (WebSocketException, OSError) as e:
                logger.debug("Closing the relay's Chromium socket was ignored ({!r})", e)
            pump.join(timeout=_PUMP_JOIN_SECONDS)

    def forward_frame(
        self,
        raw: str | bytes,
        token: str,
        reply: Callable[[str | bytes], Any],
        forward: Callable[[str | bytes], Any],
    ) -> None:
        """Screen one agent frame: refused with a CDP error reply, or forwarded with the view following."""
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        verdict = self.screen(frame, token)
        if verdict is not None:
            logger.debug("Refused a CDP frame ({}) for browser {}: {}", frame.get("method"), self.name, verdict)
            reply(cdp_error_frame(frame.get("id"), frame.get("sessionId"), verdict))
            return
        forward(raw)
        self.schedule_follow(frame)


class ProxyServer(MutableModel):
    """One WebSocket server for the whole fleet, addressed by ``/<browser-name>/<token>``, on its own loopback port:
    the Screen service's port is published to the desktop client, and an unauthenticated CDP endpoint must not
    travel with it."""

    model_config = {"arbitrary_types_allowed": True}

    host: str = Field(default=CDP_PROXY_HOST, frozen=True, description="The address to bind")
    requested_port: int = Field(default=CDP_PROXY_PORT, description="The port to bind; 0 picks a free one")

    _browsers: dict[str, BrowserProxy] = PrivateAttr(default_factory=dict)
    _connections: set[ServerConnection] = PrivateAttr(default_factory=set)
    _server: Server | None = PrivateAttr(default=None)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def port(self) -> int:
        if self._server is None:
            return self.requested_port
        return int(self._server.socket.getsockname()[1])

    @property
    def public_base(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def attach_url(self, name: str, token: str) -> str:
        return f"http://{self.host}:{self.port}/{name}/{token}"

    def register(self, proxy: BrowserProxy) -> None:
        with self._lock:
            self._browsers[proxy.name] = proxy

    def unregister(self, name: str) -> None:
        with self._lock:
            proxy = self._browsers.pop(name, None)
        if proxy is not None:
            proxy.cancel_follow()

    def lookup(self, name: str) -> BrowserProxy | None:
        with self._lock:
            return self._browsers.get(name)

    def start(self) -> None:
        """Bind and serve on a daemon thread; falls back to an ephemeral port when the fixed one is taken, since the
        CLI reads the attach URL from the service and never assumes the number."""
        try:
            server = serve(
                self._handle, self.host, self.requested_port, process_request=self._process_request, max_size=None
            )
        except OSError as e:
            logger.warning(
                "The CDP proxy could not bind port {} ({}); using an ephemeral port", self.requested_port, e
            )
            server = serve(self._handle, self.host, 0, process_request=self._process_request, max_size=None)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="screen-cdp-proxy", daemon=True)
        self._thread.start()
        logger.info("The CDP proxy listens on {}:{}", self.host, self.port)

    def stop(self) -> None:
        """Stop listening and end every live relay: the library runs each relay on a non-daemon thread, so one left
        pumping would keep the service process alive after its shutdown."""
        server = self._server
        if server is None:
            return
        server.shutdown()
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.close(WS_CLOSE_TRY_AGAIN_LATER, "the Screen service is stopping")
            except (WebSocketException, OSError) as e:
                logger.debug("Closing a relay on shutdown was ignored ({!r})", e)
        if self._thread is not None:
            self._thread.join(timeout=_SERVER_JOIN_SECONDS)
        self._server = None

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        """Serve HTTP discovery; None lets the WebSocket handshake proceed."""
        if "/json/" not in request.path:
            return None
        name, token = split_proxy_path(request.path)
        proxy = self.lookup(name)
        if proxy is None:
            return discovery_response({"error": f"no browser {name}"}, status=HTTP_NOT_FOUND)
        try:
            return proxy.rewrite_discovery(request.path, token)
        except CdpError as e:
            logger.debug("Discovery for {} failed ({})", name, e)
            return discovery_response({"error": "browser is not reachable"}, status=HTTP_BAD_GATEWAY)

    def _handle(self, client: ServerConnection) -> None:
        name, token = split_proxy_path(client.request.path if client.request is not None else "")
        proxy = self.lookup(name)
        if proxy is None:
            client.close(WS_CLOSE_POLICY_VIOLATION, "unknown browser")
            return
        with self._lock:
            self._connections.add(client)
        try:
            proxy.pump(client, token)
        except (WebSocketException, OSError, CdpError) as e:
            logger.debug("The proxy session for {} ended ({})", name, e)
        finally:
            with self._lock:
                self._connections.discard(client)
