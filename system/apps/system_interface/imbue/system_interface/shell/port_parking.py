"""Port parking: holding a stopped app's loopback port so a request for the app starts it (the stop-when-no-windows
spec, section 5).

A parked port is a listening socket on the app's registered host and port. Its first accepted connection closes
the listener (so the app can bind), asks for the app to be woken, and is answered with a loading page that
reloads itself until the app answers, so neither ``mngr forward`` nor the share gateway learns anything about
starting apps: they see a backend that answers. It reads the request only far enough to answer sensibly, never
proxies anything, and answers every method and path the same way.
"""

import html
import socket
import threading
from collections.abc import Callable
from enum import auto
from typing import Final
from urllib.parse import urlsplit

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import PortInUseError
from imbue.system_interface.shell.errors import PortParkingError
from imbue.system_interface.shell.errors import ShellError

# The hosts an app's registered URL may name for its port to be parked: the shell binds the same loopback address.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
# How long the parker waits for the request to arrive on an accepted connection before answering anyway.
_REQUEST_READ_TIMEOUT_SECONDS: Final[float] = 2.0
_REQUEST_READ_LIMIT_BYTES: Final[int] = 8192
_LISTEN_BACKLOG: Final[int] = 8
# A stopped app's page reloads every few seconds until the app answers; one that failed to start waits longer
# between attempts, since each reload is a new request and so a new wake.
STARTING_REFRESH_SECONDS: Final[int] = 2
FAILED_REFRESH_SECONDS: Final[int] = 15


class ParkedPageKind(LowerCaseStrEnum):
    """What the parker answers the request that woke the app with: that it is starting, or that it could not start."""

    STARTING = auto()
    FAILED = auto()


class ParkingTarget(FrozenModel):
    """Where a parkable app's registered URL points: the loopback host and port the shell holds while it is stopped."""

    host: str = Field(description="The loopback host the URL names")
    port: int = Field(description="The port the URL names")


@pure
def parking_target_of(url: str) -> ParkingTarget | None:
    """The host and port to park for a registered URL, or None for one the shell cannot hold (a remote host, no
    explicit port)."""
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.hostname not in _LOOPBACK_HOSTS or parsed.port is None:
        return None
    return ParkingTarget(host=parsed.hostname, port=parsed.port)


_PAGE_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="{refresh}">
<title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;color:#334155}}div{{text-align:center;max-width:32rem}}code{{font-size:0.9em}}</style></head>
<body><div><h1>{heading}</h1><p>{detail}</p></div></body></html>
"""


@pure
def parked_page_html(kind: ParkedPageKind, display_name: str, program: str) -> str:
    """The page the parker answers with: starting (reloading every few seconds) or could not start."""
    name = html.escape(display_name)
    if kind is ParkedPageKind.STARTING:
        return _PAGE_TEMPLATE.format(
            refresh=STARTING_REFRESH_SECONDS,
            title=f"Starting {name}",
            heading=f"Starting {name}&hellip;",
            detail="The app was stopped while nothing showed it. This page opens it once it answers.",
        )
    return _PAGE_TEMPLATE.format(
        refresh=FAILED_REFRESH_SECONDS,
        title=f"{name} could not start",
        heading=f"{name} could not start",
        detail=(
            f"The app's program did not stay up. Its log says why: "
            f"<code>supervisorctl tail {html.escape(program)} stderr</code>. This page tries again in a while."
        ),
    )


@pure
def parked_response_bytes(page: str) -> bytes:
    """One complete HTTP/1.1 503 answer carrying ``page``, closing the connection so nothing is pooled against
    the parker."""
    body = page.encode("utf-8")
    head = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        f"Retry-After: {STARTING_REFRESH_SECONDS}\r\n"
        "Cache-Control: no-store\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode("ascii") + body


class ParkedPort(MutableModel):
    """One stopped app's port, held until the first connection asks for the app."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    app: str = Field(frozen=True, description="The app whose port is held")
    target: ParkingTarget = Field(frozen=True, description="The host and port held")
    on_first_connection: Callable[[], ParkedPageKind] = Field(
        frozen=True,
        description="Called once, from the accept thread, after the listener is closed; answers what the page says",
    )
    page_for: Callable[[ParkedPageKind], str] = Field(
        frozen=True, description="The page for each kind, rendered when answering"
    )

    _listener: socket.socket | None = PrivateAttr(default=None)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_woken: bool = PrivateAttr(default=False)

    @property
    def is_woken(self) -> bool:
        """Whether a connection has come in and the wake was asked for; the listener is closed from then on."""
        with self._lock:
            return self._is_woken

    def start(self) -> None:
        """Bind and listen. Raises PortInUseError when something already listens there (the app, most likely),
        PortParkingError for any other bind failure."""
        family = socket.AF_INET6 if ":" in self.target.host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.target.host, self.target.port))
            listener.listen(_LISTEN_BACKLOG)
        except OSError as e:
            listener.close()
            if e.errno in (98, 48):
                raise PortInUseError(f"{self.target.host}:{self.target.port} is in use") from e
            raise PortParkingError(f"could not park {self.target.host}:{self.target.port}: {e}") from e
        self._listener = listener
        thread = threading.Thread(target=self._serve, daemon=True, name=f"parked-port-{self.app}")
        self._thread = thread
        thread.start()

    def release(self) -> None:
        """Close the listener (idempotent) and wait briefly for the accept thread."""
        with self._lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            # A close alone leaves an accept blocked in the other thread holding the port; the shutdown wakes it.
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            listener.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _serve(self) -> None:
        with self._lock:
            listener = self._listener
        if listener is None:
            return
        try:
            connection, _address = listener.accept()
        except OSError:
            # Released before anything connected.
            return
        with self._lock:
            self._is_woken = True
            self._listener = None
        # The listener goes first, so the app can bind the moment it starts; connections still queued behind it are
        # reset, and whoever sent them retries through the proxy's own loading page.
        listener.close()
        try:
            kind = self.on_first_connection()
        except (ShellError, OSError) as e:
            logger.opt(exception=e).error("The wake of {} raised; answering its request as a failed start", self.app)
            kind = ParkedPageKind.FAILED
        self._answer(connection, kind)

    def _answer(self, connection: socket.socket, kind: ParkedPageKind) -> None:
        try:
            with connection:
                connection.settimeout(_REQUEST_READ_TIMEOUT_SECONDS)
                try:
                    connection.recv(_REQUEST_READ_LIMIT_BYTES)
                except OSError:
                    pass
                connection.sendall(parked_response_bytes(self.page_for(kind)))
        except OSError as e:
            logger.debug("Could not answer the request that woke {}: {}", self.app, e)
