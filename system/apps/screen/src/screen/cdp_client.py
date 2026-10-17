"""The fleet's own CDP connection to Chromium: the browser-level channel the service needs for its bookkeeping
(the tab list, foregrounding the tab an agent acts on, reopening tabs after a start, noticing that Chromium died
with nobody attached). Agents never use it; they drive through ``cdp_proxy``, which holds its own upstream socket.
Several CDP clients coexist on one browser without interference.

Reconnect-vs-dead is the subtle part: a transient socket drop that reconnects is not a crash, only a connection
that is both down and out of retries is, and ``is_alive`` says so, which lets the crash poll keep its two-poll
debounce.
"""

import json
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as connect_websocket

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.errors import CdpError

# A CDP round trip is local and sub-millisecond in the happy path; anything slower is a stalled renderer, and the
# service must never wedge a thread waiting on one.
CALL_TIMEOUT_SECONDS: Final[float] = 5.0
_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0
_DISCOVERY_TIMEOUT_SECONDS: Final[float] = 5.0
# Chromium closing its side briefly (a heavy navigation) must not read as death, but a dead browser has to be
# reported promptly: the crash poll runs every ten seconds, so this stays well inside one tick.
_RECONNECT_ATTEMPTS: Final[int] = 3
_RECONNECT_DELAY_SECONDS: Final[float] = 0.5
_CHROME_INTERNAL_URL_PREFIXES: Final[tuple[str, ...]] = ("chrome://", "chrome-extension://", "devtools://")


class CdpTarget(FrozenModel):
    """One page target as Chromium lists it."""

    target_id: str = Field(description="The target id")
    url: str = Field(description="The page's URL")
    title: str = Field(default="", description="The page's title")


@pure
def dict_at(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    """The dict a key holds, or an empty one when it holds anything else."""
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


@pure
def is_real_page(target: dict[str, Any]) -> bool:
    """A user-visible tab, as opposed to an extension or Chrome-internal target: the filter ``playwright-cli
    tab-list`` applies, so the fleet's tab indices and the agent's agree."""
    if target.get("type") != "page":
        return False
    url = target.get("url", "")
    return isinstance(url, str) and not url.startswith(_CHROME_INTERNAL_URL_PREFIXES)


@pure
def order_targets(targets: list[dict[str, Any]], creation_order: list[str]) -> list[CdpTarget]:
    """The real pages in the order Chromium created them (``Target.getTargets`` answers in an arbitrary order);
    a target never seen created sorts last rather than being dropped."""
    position = {target_id: index for index, target_id in enumerate(creation_order)}
    pages = [target for target in targets if is_real_page(target)]
    pages.sort(key=lambda target: position.get(target["targetId"], len(position)))
    return [
        CdpTarget(target_id=target["targetId"], url=target.get("url", ""), title=target.get("title", ""))
        for target in pages
    ]


def browser_websocket_url(http_endpoint: str) -> str:
    """The browser-level WebSocket URL ``/json/version`` names."""
    try:
        response = httpx.get(f"{http_endpoint.rstrip('/')}/json/version", timeout=_DISCOVERY_TIMEOUT_SECONDS)
        response.raise_for_status()
        url = response.json().get("webSocketDebuggerUrl")
    except (httpx.HTTPError, ValueError) as e:
        raise CdpError(f"Chromium's discovery route could not be read: {e}") from e
    if not isinstance(url, str) or not url:
        raise CdpError("Chromium's discovery route named no webSocketDebuggerUrl")
    return url


class CdpClientInterface(MutableModel, ABC):
    """The fleet's bookkeeping primitives over one Chromium."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Whether the connection is usable or mid-reconnect (a socket being retried is not yet a crash)."""

    @abstractmethod
    def ping(self) -> bool:
        """A liveness probe that tries one reconnect before failing."""

    @abstractmethod
    def page_targets(self) -> list[CdpTarget]:
        """The real page targets, in Chromium's creation order."""

    @abstractmethod
    def activate(self, target_id: str) -> None:
        """Bring a tab to the front of its window."""

    @abstractmethod
    def create_target(self, url: str) -> str:
        """Open a new tab on ``url`` and answer its target id."""

    @abstractmethod
    def close_target(self, target_id: str) -> None:
        """Close a tab."""

    @abstractmethod
    def is_shown(self, target_id: str) -> bool:
        """Whether a tab is the one in front of its window, asked of the page itself."""

    @abstractmethod
    def close(self) -> None:
        """Drop the connection."""


class CdpConnectorInterface(MutableModel, ABC):
    """How the fleet opens its CDP connection to a launched Chromium."""

    @abstractmethod
    def connect(self, http_endpoint: str) -> CdpClientInterface:
        """Connect to the browser-level socket ``http_endpoint`` names; raises ``CdpError`` when it cannot."""


class _PendingCall:
    """One call awaiting Chromium's reply."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.reply: dict[str, Any] | None = None


class CdpClient(CdpClientInterface):
    """A browser-level CDP connection over a synchronous WebSocket: a reader thread dispatches replies to the
    calls awaiting them and tracks the tab order from the target lifecycle events."""

    model_config = {"arbitrary_types_allowed": True}

    http_endpoint: str = Field(frozen=True, description="Chromium's debug HTTP origin")

    _connection: ClientConnection | None = PrivateAttr(default=None)
    _reader: threading.Thread | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _next_id: int = PrivateAttr(default=0)
    _pending: dict[int, _PendingCall] = PrivateAttr(default_factory=dict)
    _creation_order: list[str] = PrivateAttr(default_factory=list)
    _is_reconnecting: bool = PrivateAttr(default=False)
    _is_closed: bool = PrivateAttr(default=False)
    _pause: threading.Event = PrivateAttr(default_factory=threading.Event)

    def connect(self) -> None:
        url = browser_websocket_url(self.http_endpoint)
        try:
            connection = connect_websocket(url, max_size=None, open_timeout=_CONNECT_TIMEOUT_SECONDS)
        except (WebSocketException, OSError) as e:
            raise CdpError(f"could not open Chromium's browser socket: {e}") from e
        with self._lock:
            self._connection = connection
        reader = threading.Thread(target=self._read_loop, args=(connection,), name="screen-cdp-reader", daemon=True)
        self._reader = reader
        reader.start()
        # Replays targetCreated for everything already open, then streams new ones.
        self.send("Target.setDiscoverTargets", {"discover": True})

    def close(self) -> None:
        self._is_closed = True
        with self._lock:
            connection = self._connection
            self._connection = None
        if connection is not None:
            try:
                connection.close()
            except (WebSocketException, OSError) as e:
                logger.debug("Closing the CDP connection was ignored ({})", e)

    def is_alive(self) -> bool:
        if self._is_closed:
            return False
        if self._is_reconnecting:
            return True
        with self._lock:
            connection = self._connection
        return connection is not None and connection.close_code is None

    def _read_loop(self, connection: ClientConnection) -> None:
        try:
            for raw in connection:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    with self._lock:
                        pending = self._pending.pop(message["id"], None)
                    if pending is not None:
                        pending.reply = message
                        pending.done.set()
                    continue
                self._track_target(message)
        except (WebSocketException, OSError, ValueError) as e:
            logger.debug("The CDP read loop ended ({})", e)
        finally:
            with self._lock:
                pending_calls, self._pending = list(self._pending.values()), {}
            for pending in pending_calls:
                pending.done.set()

    def _track_target(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = dict_at(message, "params")
        if method == "Target.targetCreated":
            target_id = dict_at(params, "targetInfo").get("targetId")
            if isinstance(target_id, str) and target_id and target_id not in self._creation_order:
                self._creation_order.append(target_id)
        elif method == "Target.targetDestroyed":
            target_id = params.get("targetId")
            if target_id in self._creation_order:
                self._creation_order.remove(target_id)
        else:
            pass

    def _reconnect(self) -> bool:
        if self._is_closed or self._is_reconnecting:
            return False
        self._is_reconnecting = True
        try:
            for _ in range(_RECONNECT_ATTEMPTS):
                self._pause.wait(_RECONNECT_DELAY_SECONDS)
                try:
                    self.connect()
                    return True
                except CdpError as e:
                    logger.debug("A CDP reconnect attempt failed ({})", e)
            return False
        finally:
            self._is_reconnecting = False

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout_seconds: float = CALL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """One CDP call; answers its ``result`` or raises ``CdpError``."""
        pending = _PendingCall()
        with self._lock:
            connection = self._connection
            self._next_id += 1
            call_id = self._next_id
            frame: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
            if session_id:
                frame["sessionId"] = session_id
            self._pending[call_id] = pending
        if connection is None:
            with self._lock:
                self._pending.pop(call_id, None)
            raise CdpError(f"{method}: no connection")
        try:
            connection.send(json.dumps(frame))
        except (WebSocketException, OSError) as e:
            with self._lock:
                self._pending.pop(call_id, None)
            raise CdpError(f"{method}: send failed ({e})") from e
        if not pending.done.wait(timeout_seconds):
            with self._lock:
                self._pending.pop(call_id, None)
            raise CdpError(f"{method}: timed out")
        reply = pending.reply
        if reply is None:
            raise CdpError(f"{method}: the connection closed")
        if "error" in reply:
            raise CdpError(f"{method}: {reply['error']}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def ping(self) -> bool:
        try:
            self.send("Browser.getVersion")
            return True
        except CdpError as e:
            logger.debug("The CDP ping failed ({}); attempting a reconnect", e)
        if not self._reconnect():
            return False
        try:
            self.send("Browser.getVersion")
            return True
        except CdpError as e:
            logger.debug("The CDP ping failed after the reconnect ({})", e)
            return False

    def page_targets(self) -> list[CdpTarget]:
        result = self.send("Target.getTargets")
        infos = result.get("targetInfos")
        return order_targets(infos if isinstance(infos, list) else [], list(self._creation_order))

    def activate(self, target_id: str) -> None:
        self.send("Target.activateTarget", {"targetId": target_id})

    def create_target(self, url: str) -> str:
        target_id = self.send("Target.createTarget", {"url": url}).get("targetId")
        if not isinstance(target_id, str) or not target_id:
            raise CdpError("Target.createTarget answered no target id")
        return target_id

    def close_target(self, target_id: str) -> None:
        self.send("Target.closeTarget", {"targetId": target_id})

    def is_shown(self, target_id: str) -> bool:
        with self._attached(target_id) as session_id:
            result = self.send(
                "Runtime.evaluate",
                {"expression": 'document.visibilityState === "visible"', "returnByValue": True},
                session_id=session_id,
            )
        value = result.get("result")
        return isinstance(value, dict) and value.get("value") is True

    @contextmanager
    def _attached(self, target_id: str) -> Generator[str]:
        """A flattened session on one target for the page-domain calls of the block, dropped on the way out."""
        attached = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = attached.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise CdpError("Target.attachToTarget answered no session id")
        try:
            yield session_id
        finally:
            try:
                self.send("Target.detachFromTarget", {"sessionId": session_id})
            except CdpError as e:
                logger.debug("Detaching the CDP session {} was ignored ({})", session_id, e)


class WebSocketCdpConnector(CdpConnectorInterface):
    """Opens a ``CdpClient`` to a launched Chromium."""

    def connect(self, http_endpoint: str) -> CdpClientInterface:
        client = CdpClient(http_endpoint=http_endpoint)
        client.connect()
        return client
