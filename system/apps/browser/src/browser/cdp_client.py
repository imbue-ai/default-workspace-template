"""The fleet's own persistent CDP connection to one Chromium.

Replaces what browser-use's `BrowserSession` provided: the browser-level channel the
fleet needs for its *own* bookkeeping -- listing tabs, foregrounding the tab the pane
films, restoring tabs after a restart, and (§5.1) noticing that Chromium died even when
no agent is attached.

This is deliberately NOT the agent's channel. Agents talk to `cdp_proxy`, which holds its
own upstream socket. Three simultaneous CDP clients on one browser were measured to
coexist without interference, so the fleet keeping a private one is safe.

Reconnect-vs-dead is the subtle part and the reason this class exists rather than a bare
`websockets.connect()`. A transient socket drop that reconnects is NOT a crash; only a
connection that is both down and out of retries is. `alive` encodes that, so the keepalive
loop can keep its two-poll debounce (see `LiveBrowser._keepalive_loop`).
"""

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

import websockets
from loguru import logger

# A CDP round-trip is local and sub-millisecond in the happy path; anything slower is a
# stalled renderer, and the fleet must never wedge its event loop waiting on one.
_CALL_TIMEOUT_S = 5.0
_CONNECT_TIMEOUT_S = 10.0
# Reconnect budget. Chromium closing its side briefly (e.g. during a heavy navigation)
# must not read as death, but an actually-dead browser has to be reported promptly --
# the keepalive loop polls every 10s, so this stays well inside one tick.
_RECONNECT_ATTEMPTS = 3
_RECONNECT_DELAY_S = 0.5


class CdpError(RuntimeError):
    """A CDP call returned an error payload, or could not be delivered."""


class CdpClient:
    """One browser-level CDP connection, with bounded reconnect."""

    def __init__(self, http_endpoint: str) -> None:
        self._http = http_endpoint.rstrip("/")
        self._ws: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._reconnecting = False
        self._closed = False
        self._lock = asyncio.Lock()
        # Tab order, and it has to be tracked rather than read. `Target.getTargets` returns
        # targets in an ARBITRARY order -- measured: tabs created com, org, net came back
        # net, com, org. Indexing into that list restores the wrong tab after a restart and
        # makes `ls --include-tabs` disagree with the strip the human is looking at.
        # `Target.setDiscoverTargets` gives us targetCreated/targetDestroyed instead, so we
        # keep the order Chrome actually made them in.
        self._order: list[str] = []

    # --- lifecycle -------------------------------------------------------

    @staticmethod
    def browser_ws_url(http_endpoint: str) -> str:
        """Resolve the browser-level websocket URL from the HTTP endpoint."""
        with urllib.request.urlopen(f"{http_endpoint.rstrip('/')}/json/version", timeout=5) as r:
            return json.loads(r.read())["webSocketDebuggerUrl"]

    async def connect(self) -> None:
        url = await asyncio.to_thread(self.browser_ws_url, self._http)
        self._ws = await asyncio.wait_for(
            websockets.connect(url, max_size=None, ping_interval=None), timeout=_CONNECT_TIMEOUT_S
        )
        self._reader = asyncio.create_task(self._read_loop())
        # Replays targetCreated for everything already open, then streams new ones.
        await self.send("Target.setDiscoverTargets", {"discover": True})

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:  # noqa: BLE001  (teardown is best-effort)
                logger.debug("cdp client close ignored ({})", e)

    @property
    def alive(self) -> bool:
        """Whether the connection is usable OR mid-reconnect.

        Mirrors the guarantee `browser_use`'s `_bu_alive()` gave: a socket that is down
        but still being retried is not yet a crash. `LiveBrowser` debounces on top of
        this, so a genuinely dead browser is reported within two keepalive ticks.
        """
        if self._closed:
            return False
        if self._reconnecting:
            return True
        ws = self._ws
        return ws is not None and getattr(ws, "close_code", None) is None

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue
                self._track_target(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  (any read failure means the socket is gone)
            logger.debug("cdp read loop ended ({})", e)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CdpError("connection closed"))
            self._pending.clear()

    def _track_target(self, msg: dict[str, Any]) -> None:
        """Maintain tab order from the target lifecycle events."""
        method = msg.get("method")
        if method == "Target.targetCreated":
            target_id = (msg.get("params", {}).get("targetInfo") or {}).get("targetId")
            if target_id and target_id not in self._order:
                self._order.append(target_id)
        elif method == "Target.targetDestroyed":
            target_id = msg.get("params", {}).get("targetId")
            if target_id in self._order:
                self._order.remove(target_id)

    async def _reconnect(self) -> bool:
        """Best-effort reconnect. Returns True if the socket is usable again."""
        if self._closed or self._reconnecting:
            return False
        self._reconnecting = True
        try:
            for _ in range(_RECONNECT_ATTEMPTS):
                await asyncio.sleep(_RECONNECT_DELAY_S)
                try:
                    await self.connect()
                    return True
                except Exception as e:  # noqa: BLE001  (retry until the budget runs out)
                    logger.debug("cdp reconnect attempt failed ({})", e)
            return False
        finally:
            self._reconnecting = False

    # --- calls -----------------------------------------------------------

    async def send(self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            self._next_id += 1
            call_id = self._next_id
            frame: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
            if session_id:
                frame["sessionId"] = session_id
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[call_id] = fut
            try:
                await self._ws.send(json.dumps(frame))
            except Exception as e:  # noqa: BLE001
                self._pending.pop(call_id, None)
                raise CdpError(f"{method}: send failed ({e})") from e
        try:
            msg = await asyncio.wait_for(fut, timeout=_CALL_TIMEOUT_S)
        except TimeoutError as e:
            self._pending.pop(call_id, None)
            raise CdpError(f"{method}: timed out") from e
        if "error" in msg:
            raise CdpError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def ping(self) -> bool:
        """Liveness probe for the keepalive loop. Attempts one reconnect before failing."""
        try:
            await self.send("Browser.getVersion")
            return True
        except Exception as e:  # noqa: BLE001  (any failure is a liveness signal, not a raise)
            logger.debug("cdp ping failed ({}), attempting reconnect", e)
            if await self._reconnect():
                try:
                    await self.send("Browser.getVersion")
                    return True
                except Exception as e2:  # noqa: BLE001
                    logger.debug("cdp ping failed after reconnect ({})", e2)
            return False

    # --- the fleet's bookkeeping primitives -------------------------------

    async def page_targets(self) -> list[dict[str, Any]]:
        """Real page targets, in Chromium's order.

        Filters exactly what `playwright-cli tab-list` filters, so the fleet's `ls`
        indices and the agent's `tab-select` indices cannot disagree: a fresh profile
        with ONE page reports five targets (two `chrome://` browser_ui, one extension
        background_page, one service_worker, one page).
        """
        result = await self.send("Target.getTargets")
        pages = [t for t in result.get("targetInfos", []) if _is_real_page(t)]
        # Creation order, not getTargets order (see `_order`). Anything we somehow never saw
        # created sorts last rather than being dropped.
        position = {tid: i for i, tid in enumerate(self._order)}
        return sorted(pages, key=lambda t: position.get(t["targetId"], len(position)))

    async def activate(self, target_id: str) -> None:
        await self.send("Target.activateTarget", {"targetId": target_id})

    async def create_target(self, url: str) -> str:
        return (await self.send("Target.createTarget", {"url": url}))["targetId"]

    async def close_target(self, target_id: str) -> None:
        await self.send("Target.closeTarget", {"targetId": target_id})

    async def navigate(self, target_id: str, url: str) -> None:
        """Point one tab at ``url`` (the instances API's location verb).

        ``Page.navigate`` is a page-domain call, so it needs a session on the target: attach
        flattened, navigate through that session, detach. Chromium reports a navigation it
        could not even start (a bad host, a refused scheme) in ``errorText`` rather than as a
        protocol error, so that is raised too.
        """
        attached = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = attached["sessionId"]
        try:
            result = await self.send("Page.navigate", {"url": url}, session_id=session_id)
        finally:
            await self._detach_quietly(session_id)
        error_text = result.get("errorText")
        if error_text:
            raise CdpError(f"Page.navigate to {url}: {error_text}")

    async def _detach_quietly(self, session_id: str) -> None:
        """Drop a session opened for one call; a detach that fails changes nothing for the caller."""
        try:
            await self.send("Target.detachFromTarget", {"sessionId": session_id})
        except CdpError as e:
            logger.debug("cdp detach of session {} ignored ({})", session_id, e)


def _is_real_page(target: dict[str, Any]) -> bool:
    """A user-visible tab, as opposed to an extension or Chrome-internal target."""
    if target.get("type") != "page":
        return False
    url = target.get("url", "")
    return not url.startswith(("chrome://", "chrome-extension://", "devtools://"))
