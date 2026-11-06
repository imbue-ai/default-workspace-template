"""The reverse proxy to neko under ``/neko/`` (docs/system/specs/screen/spec.md D12): HTTP requests relayed over
httpx, the WebSocket relayed message by message between the viewer's socket and an upstream client, two threads per
viewer, and a rate limit on neko's login route."""

import threading
import time
from collections.abc import Iterator
from typing import Any
from typing import Final

import httpx
from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from simple_websocket import ConnectionClosed
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as connect_websocket

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.neko_client import NEKO_BASE_URL
from screen.neko_client import NEKO_WEBSOCKET_URL

NEKO_PROXY_PREFIX: Final[str] = "/neko"
NEKO_LOGIN_PATH: Final[str] = "api/login"

# Headers that belong to one hop and never cross the proxy; ``Origin`` stays behind too, since neko only admits a
# WebSocket whose origin it allows or which carries none.
_HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "origin",
    }
)

_UPSTREAM_TIMEOUT_SECONDS: Final[float] = 60.0
_WEBSOCKET_OPEN_TIMEOUT_SECONDS: Final[float] = 10.0
_DOWNSTREAM_POLL_SECONDS: Final[float] = 0.5
_UPSTREAM_POLL_SECONDS: Final[float] = 0.5
_STREAM_CHUNK_BYTES: Final[int] = 65536

# Nothing legitimate logs in through the proxy (the service logs humans in itself), so the budget is small.
LOGIN_RATE_LIMIT_COUNT: Final[int] = 10
LOGIN_RATE_LIMIT_WINDOW_SECONDS: Final[float] = 60.0
HTTP_TOO_MANY_REQUESTS: Final[int] = 429
HTTP_BAD_GATEWAY: Final[int] = 502


@pure
def forwardable_headers(headers: Iterator[tuple[str, str]]) -> dict[str, str]:
    return {name: value for name, value in headers if name.lower() not in _HOP_BY_HOP_HEADERS}


class LoginRateLimiter(MutableModel):
    """A sliding window over the proxied login attempts."""

    limit: int = Field(default=LOGIN_RATE_LIMIT_COUNT, frozen=True, description="Attempts allowed per window")
    window_seconds: float = Field(default=LOGIN_RATE_LIMIT_WINDOW_SECONDS, frozen=True, description="The window")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _attempts: list[float] = PrivateAttr(default_factory=list)

    def allow(self, now: float) -> bool:
        with self._lock:
            self._attempts = [moment for moment in self._attempts if now - moment < self.window_seconds]
            if len(self._attempts) >= self.limit:
                return False
            self._attempts.append(now)
            return True


class NekoProxy(MutableModel):
    """Relays one request or one WebSocket to neko."""

    model_config = {"arbitrary_types_allowed": True}

    base_url: str = Field(default=NEKO_BASE_URL, frozen=True, description="neko's HTTP origin")
    websocket_url: str = Field(default=NEKO_WEBSOCKET_URL, frozen=True, description="neko's WebSocket")
    login_limiter: LoginRateLimiter = Field(default_factory=LoginRateLimiter, description="The login budget")

    _client: httpx.Client = PrivateAttr(default_factory=lambda: httpx.Client(timeout=_UPSTREAM_TIMEOUT_SECONDS))

    def relay_http(self, request: Request, upstream_path: str) -> Response:
        """Forward a request under ``/neko/`` and stream neko's answer back."""
        if upstream_path == NEKO_LOGIN_PATH and not self.login_limiter.allow(time.monotonic()):
            return Response(
                '{"message":"too many login attempts"}', status=HTTP_TOO_MANY_REQUESTS, mimetype="application/json"
            )
        upstream_url = f"{self.base_url}/{upstream_path}"
        if request.query_string:
            upstream_url += "?" + request.query_string.decode("latin-1")
        upstream_request = self._client.build_request(
            request.method,
            upstream_url,
            headers=forwardable_headers(iter(request.headers.items())),
            content=request.get_data(),
        )
        try:
            upstream_response = self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as e:
            logger.debug("neko could not be reached for {} {}: {}", request.method, upstream_path, e)
            return Response(
                '{"message":"neko is not reachable"}', status=HTTP_BAD_GATEWAY, mimetype="application/json"
            )

        def body() -> Iterator[bytes]:
            try:
                yield from upstream_response.iter_raw(_STREAM_CHUNK_BYTES)
            finally:
                upstream_response.close()

        response_headers = forwardable_headers(iter(upstream_response.headers.items()))
        return Response(body(), status=upstream_response.status_code, headers=response_headers)

    def relay_websocket(self, downstream: Any, query_string: str) -> None:
        """Relay a viewer's WebSocket to neko's: this thread carries viewer-to-neko, a second thread the other way,
        and either side closing ends both."""
        upstream_url = self.websocket_url + (f"?{query_string}" if query_string else "")
        try:
            upstream = connect_websocket(upstream_url, open_timeout=_WEBSOCKET_OPEN_TIMEOUT_SECONDS)
        except (WebSocketException, OSError) as e:
            logger.debug("neko's WebSocket could not be opened: {}", e)
            downstream.close(1013)
            return
        stop = threading.Event()

        def pump_upstream_to_downstream() -> None:
            try:
                while not stop.is_set():
                    try:
                        message = upstream.recv(timeout=_UPSTREAM_POLL_SECONDS)
                    except TimeoutError:
                        continue
                    downstream.send(message)
            except (WebSocketException, ConnectionClosed, OSError) as e:
                logger.debug("The neko relay's upstream-to-viewer side ended: {!r}", e)
            finally:
                stop.set()

        pump = threading.Thread(target=pump_upstream_to_downstream, name="screen-neko-ws-pump", daemon=True)
        pump.start()
        try:
            while not stop.is_set():
                message = downstream.receive(timeout=_DOWNSTREAM_POLL_SECONDS)
                if message is None:
                    continue
                upstream.send(message)
        except (ConnectionClosed, WebSocketException, OSError) as e:
            logger.debug("The neko relay's viewer-to-upstream side ended: {!r}", e)
        finally:
            stop.set()
            try:
                upstream.close()
            except (WebSocketException, OSError) as e:
                logger.debug("Could not close the neko relay's upstream socket cleanly: {!r}", e)
            pump.join(timeout=5.0)
