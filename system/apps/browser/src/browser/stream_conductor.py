"""The stream conductor: the single authority for which ONE viewer streams at a time.

Encoding a browser to a viewer costs real CPU (software H.264, ~half a core) and
bandwidth, and a workspace can have several browsers open across several clients (a
laptop and a phone). Rather than let every open viewer encode -- which multiplies that
cost and, unbounded, is a trivial DoS -- exactly one viewer streams: the one the user is
attending to. Every other viewer (other browsers, other panes, other devices) is fully
paused: its capture is STOPPED, so ~0 CPU and 0 bandwidth, resuming in ~40ms.

This is deliberately shaped like the other single-owner mechanisms in the pipe (one
sender thread owns all sends; one mailbox arbitrates what ships): one conductor owns the
"who is active" decision. Viewers do not negotiate among themselves -- each just reports
``interact`` (the user is attending to me) or ``hidden`` (I'm off-screen) to the
conductor, and the conductor alone flips exactly one connection active and the rest
paused. Because the daemon is the single point every client connects to, this
generalizes across devices for free, and it orders by the conductor's own receive time,
so mismatched client clocks never matter.

The conductor never touches a pipe directly. It only sets each connection's ``active``
flag and wakes that connection's own sender loop; the actual (slower) pause/resume of the
capture runs there, serialized on one thread per connection -- so transitions can't race
and the ">= no more than one live encoder" invariant holds without holding a lock across
the slow stop/start.
"""

import threading
from typing import Callable

from loguru import logger


class StreamConnection:
    """One ``/stream`` connection's handle. ``active`` is the DESIRED state (set = this
    connection should be the one streaming); the connection's sender loop reconciles it
    against the live pipe. ``wake`` nudges that loop when the flag changes."""

    def __init__(self, browser_id: str, wake: Callable[[], None]) -> None:
        self.browser_id = browser_id
        self.active = threading.Event()  # cleared = paused (the default until it claims)
        self._wake = wake

    def wake(self) -> None:
        try:
            self._wake()
        except Exception as error:  # noqa: BLE001  (waking is best-effort; never fatal)
            logger.debug("stream conductor wake failed ({})", error)


class StreamConductor:
    """The single active-viewer authority across all browsers and all clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: "StreamConnection | None" = None

    def interact(self, conn: StreamConnection) -> None:
        """The user is attending to ``conn``: make it the sole active viewer, pausing the
        previous one. Ordered by call arrival, so the most recent interaction (from any
        device) wins."""
        to_wake: list[StreamConnection] = []
        with self._lock:
            if self._active is conn and conn.active.is_set():
                return
            previous = self._active
            self._active = conn
            conn.active.set()
            to_wake.append(conn)
            if previous is not None and previous is not conn:
                previous.active.clear()
                to_wake.append(previous)
        for other in to_wake:
            other.wake()

    def hidden(self, conn: StreamConnection) -> None:
        """``conn`` went off-screen (tab switched away, window backgrounded): pause it. If
        it was the active one, nothing is active now (0 encoders anywhere)."""
        with self._lock:
            conn.active.clear()
            if self._active is conn:
                self._active = None
        conn.wake()

    def leave(self, conn: StreamConnection) -> None:
        """``conn`` disconnected: forget it (its pipe tears down on its own)."""
        with self._lock:
            conn.active.clear()
            if self._active is conn:
                self._active = None


# Process-wide singleton; every serve_stream connection reports to this one conductor.
conductor = StreamConductor()
