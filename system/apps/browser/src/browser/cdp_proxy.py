"""The agent's gated CDP endpoint.

Agents do not get Chromium's real debug port; they get `ws://127.0.0.1:<proxy>/<name>/<token>`.
Everything an agent sends passes through `_forward`, which is where the ownership lease is
enforced -- the role `LiveBrowser.run_action()`'s compare-and-set played when the fleet owned
the driving verbs.

Two design points are load-bearing and were settled by measurement, not reasoning:

* **Refuse frames; never close the socket.** A `@playwright/cli` slug is poisoned the moment
  its socket drops: it never rebinds, and `playwright-cli list` stops showing it. Refusing
  individual frames was measured to leave the session healthy -- the same slug drove normally
  again as soon as the lease came back.
* **Serve discovery on both the bare and trailing-slash paths.** Playwright fetches
  `/json/version/`; a proxy that routes only `/json/version` fails the attach with
  `Unexpected status 404` and the CLI daemon exits.

This is a guardrail, not a security boundary. The agent has a shell and can read
`DevToolsActivePort` out of the profile directory, so the block list is sized for what the
CLI actually sends rather than for a hostile client.
"""

import asyncio
import json
import urllib.request
from typing import Any
from typing import Awaitable
from typing import Callable

import websockets
from loguru import logger
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

# Methods refused even while the agent legitimately holds the lease. Deliberately short and
# enumerated: a general-purpose CDP filter breaks the CLI in ways nobody can predict.
_ALWAYS_BLOCKED = {
    # Kills a browser the human may be holding and destroys a fleet slot behind the fleet's
    # back. Measured: the CLI's own `close` does NOT send this, so it is insurance against a
    # future CLI change rather than a live threat.
    "Browser.close",
    # An incognito context has no profile persistence (logins silently do not survive) and its
    # targets are invisible to the fleet's tab list and to the pane. Measured: `attach` uses the
    # default context, so this is also insurance.
    "Target.createBrowserContext",
    "Target.disposeBrowserContext",
    # Overrides the PAGE viewport independently of the OS window. `window_guardian` re-pins X
    # windows and would never undo this, so the 1:1 window->capture mapping would stay broken
    # silently. Not observed from the CLI; kept because the failure mode is invisible.
    "Emulation.setDeviceMetricsOverride",
    # Redirects downloads to an arbitrary path inside the workspace.
    "Browser.setDownloadBehavior",
    "Page.setDownloadBehavior",
    # Low severity -- `window_guardian` re-pins the window to the capture region every 1.0s
    # and the real resize path is X11, so the worst case is <=1s of wrong geometry that
    # self-heals. Listed because it costs one line and removes the flicker entirely.
    "Browser.setWindowBounds",
}

# Refused only when it would close the browser's last remaining page.
_LAST_PAGE_GUARDED = {"Target.closeTarget", "Page.close"}

# Pane-follow debounce. CDP is orders of magnitude chattier than one call per verb, so the
# "agent acts -> pane follows" foregrounding is coalesced rather than fired per frame.
_FOLLOW_DEBOUNCE_S = 0.25


_LEASE_LOST = (
    "browser is not yours right now -- the human took control, or your lease expired. "
    "Run `agentic-browser-fleet ls` to see who holds it."
)


def _error(frame_id: Any, session_id: str | None, message: str, code: int = -32000) -> str:
    reply: dict[str, Any] = {"id": frame_id, "error": {"code": code, "message": message}}
    if session_id:
        reply["sessionId"] = session_id
    return json.dumps(reply)


class BrowserProxy:
    """Proxies one fleet browser. Owned by `LiveBrowser`; created when Chromium launches."""

    def __init__(
        self,
        *,
        name: str,
        upstream_http: str,
        public_base: str,
        is_allowed: Callable[[str], Awaitable[bool]],
        on_attach: Callable[[], Awaitable[None]],
        on_activity: Callable[[str | None], Awaitable[None]],
        page_count: Callable[[], Awaitable[int]],
    ) -> None:
        self.name = name
        self._upstream_http = upstream_http.rstrip("/")
        self._public_base = public_base.rstrip("/")
        self._is_allowed = is_allowed          # token -> may this token drive right now? (async)
        self._on_attach = on_attach            # first attach surfaces the pane
        self._on_activity = on_activity        # pane-follow, debounced by the caller
        self._page_count = page_count          # for the last-page guard
        self._sessions: dict[str, str] = {}    # CDP sessionId -> targetId
        self._follow_handle: asyncio.TimerHandle | None = None
        self._attached_once = False

    # --- HTTP discovery ---------------------------------------------------

    async def rewrite_discovery(self, path: str, token: str) -> Response | None:
        """Serve `/json/version[/]` and `/json/list[/]` with our URLs substituted.

        Async because `_fetch` is a blocking HTTP call to Chromium and this runs on the one
        background loop that also carries video, telemetry, keepalive and ownership --
        a slow or wedged browser must not freeze all of it for the 5s timeout.
        """
        bare = path.rstrip("/")
        if bare.endswith("/json/version"):
            payload = await asyncio.to_thread(self._fetch, "/json/version")
            payload["webSocketDebuggerUrl"] = f"{self._public_base}/{self.name}/{token}"
            return self._json(payload)
        if bare.endswith("/json/list"):
            raw = await asyncio.to_thread(self._fetch, "/json/list")
            targets = [t for t in raw if t.get("type") == "page"]
            for t in targets:
                t.pop("devtoolsFrontendUrl", None)
                t["webSocketDebuggerUrl"] = f"{self._public_base}/{self.name}/{token}"
            return self._json(targets)
        # The legacy mutating HTTP endpoints bypass the websocket filter entirely.
        if any(bare.endswith(f"/json/{verb}") for verb in ("new", "close", "activate")):
            return self._json({"error": "not available through the fleet proxy; use CDP"}, status=405)
        return None

    def _fetch(self, endpoint: str) -> Any:
        with urllib.request.urlopen(f"{self._upstream_http}{endpoint}", timeout=5) as r:
            return json.loads(r.read())

    @staticmethod
    def _json(payload: Any, status: int = 200) -> Response:
        """A discovery response, and it MUST close the connection.

        Without `Connection: close` the client keeps the socket alive and sends the
        websocket upgrade on it -- but this server has already finished with that
        connection, so the upgrade hangs up (`socket hang up`, code 1006) and the CLI
        daemon exits 1. Measured against real `@playwright/cli`: discovery succeeds, the
        attach then fails, and the proxy never even logs the upgrade. One header.
        """
        body = json.dumps(payload).encode()
        headers = Headers({
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Connection": "close",
        })
        return Response(status, "OK" if status == 200 else "Error", headers, body)

    # --- the websocket path ----------------------------------------------

    async def pump(self, client: Any, token: str) -> None:
        """Bridge one agent socket to Chromium, gating every frame the agent sends."""
        if not await self._is_allowed(token):
            # Refuse before opening an upstream socket or spending the one-shot pane
            # surfacing on somebody who cannot drive.
            await client.send(_error(None, None, "this browser is not yours -- run `agentic-browser-fleet ls`"))
            return
        upstream_url = await asyncio.to_thread(
            lambda: json.loads(urllib.request.urlopen(f"{self._upstream_http}/json/version", timeout=5).read())[
                "webSocketDebuggerUrl"
            ]
        )
        async with websockets.connect(upstream_url, max_size=None, ping_interval=None) as upstream:
            if not self._attached_once:
                self._attached_once = True
                await self._on_attach()  # surface the pane on first attach, as `_action` used to
            # FIRST_COMPLETED, not gather: when the agent detaches, `_client_to_browser`
            # returns but `_browser_to_client` stays parked in `async for` until the next
            # CDP event -- which never comes on an idle browser (ping_interval=None). That
            # would leak the upstream socket and two tasks per attach/detach cycle.
            tasks = [
                asyncio.create_task(self._client_to_browser(client, upstream, token)),
                asyncio.create_task(self._browser_to_client(client, upstream)),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _client_to_browser(self, client: Any, upstream: Any, token: str) -> None:
        async for raw in client:
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            verdict = await self._screen(frame, token)
            if verdict is not None:
                await client.send(_error(frame.get("id"), frame.get("sessionId"), verdict))
                continue
            await upstream.send(raw)
            self._schedule_follow(frame)

    async def _browser_to_client(self, client: Any, upstream: Any) -> None:
        async for raw in upstream:
            try:
                msg = json.loads(raw)
                if msg.get("method") == "Target.attachedToTarget":
                    params = msg.get("params", {})
                    session = params.get("sessionId")
                    target = (params.get("targetInfo") or {}).get("targetId")
                    if session and target:
                        self._sessions[session] = target
                elif msg.get("method") == "Target.detachedFromTarget":
                    self._sessions.pop(msg.get("params", {}).get("sessionId", ""), None)
            except ValueError:
                pass
            await client.send(raw)

    async def _screen(self, frame: dict[str, Any], token: str) -> str | None:
        """Reason to refuse this frame, or None to forward it."""
        method = frame.get("method", "")
        if not await self._is_allowed(token):
            # The lease moved (a human took control, it expired, or another agent holds it).
            # Refuse rather than disconnect: a dropped socket poisons the CLI session forever.
            return _LEASE_LOST
        if method in _ALWAYS_BLOCKED:
            return f"{method} is not permitted through the fleet proxy"
        if method in _LAST_PAGE_GUARDED:
            last_page = await self._page_count() <= 1
            # `_page_count` is a real CDP round trip, so the lease may have moved while we
            # waited. Every other method reaches the forward without suspending; this one
            # must re-check, or a human take-control during the round trip still lands.
            if not await self._is_allowed(token):
                return _LEASE_LOST
            if last_page:
                return (
                    f"{method} would close the browser's last page. Use "
                    "`agentic-browser-fleet close` to end the browser instead."
                )
        return None

    def _schedule_follow(self, frame: dict[str, Any]) -> None:
        """After any forwarded command, foreground the target it addressed (debounced).

        `run_action` used to call `_foreground_active()` after every action, and that single
        call is the whole 'agent acts -> pane follows' behavior. Direct CDP has no such hook,
        and nothing fails without it -- the human's view just silently goes stale.
        """
        if not frame.get("method"):
            return
        target = self._sessions.get(frame.get("sessionId", ""))
        loop = asyncio.get_running_loop()
        if self._follow_handle is not None:
            self._follow_handle.cancel()
        self._follow_handle = loop.call_later(
            _FOLLOW_DEBOUNCE_S, lambda: asyncio.ensure_future(self._on_activity(target))
        )


class ProxyServer:
    """One websocket server for the whole fleet, addressed by `/<browser-name>/<token>`."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._browsers: dict[str, BrowserProxy] = {}
        self._server: Any = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return next(iter(self._server.sockets)).getsockname()[1]

    def register(self, proxy: BrowserProxy) -> None:
        self._browsers[proxy.name] = proxy

    def unregister(self, name: str) -> None:
        self._browsers.pop(name, None)

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        parts = [p for p in path.split("?")[0].split("/") if p]
        if len(parts) >= 2 and parts[0] not in ("json",):
            return parts[0], parts[1]
        if len(parts) >= 3:  # /<name>/<token>/json/version
            return parts[0], parts[1]
        return "", ""

    async def start(self) -> None:
        self._server = await serve(
            self._handle, self._host, self._port, process_request=self._process_request, max_size=None
        )
        logger.info("CDP proxy listening on {}:{}", self._host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _process_request(self, connection: Any, request: Any) -> Response | None:
        """Serve HTTP discovery; return None to let the websocket handshake proceed."""
        path = request.path
        if "/json/" not in path:
            return None
        name, token = self._split(path)
        proxy = self._browsers.get(name)
        if proxy is None:
            return BrowserProxy._json({"error": f"no browser {name}"}, status=404)
        try:
            return await proxy.rewrite_discovery(path, token)
        except Exception as e:  # noqa: BLE001  (a dead upstream must not 500 the whole server)
            logger.debug("discovery for {} failed ({})", name, e)
            return BrowserProxy._json({"error": "browser is not reachable"}, status=502)

    async def _handle(self, client: Any) -> None:
        name, token = self._split(client.request.path)
        proxy = self._browsers.get(name)
        if proxy is None:
            await client.close(code=1008, reason="unknown browser")
            return
        try:
            await proxy.pump(client, token)
        except Exception as e:  # noqa: BLE001  (one bad session must not take the server down)
            logger.debug("proxy session for {} ended ({})", name, e)
