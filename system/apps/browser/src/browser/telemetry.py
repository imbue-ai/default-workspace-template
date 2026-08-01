"""Passive, watch-only telemetry for the pixelflux stream (Rung 1: server-side).

The pipe already computes almost everything an engineer needs to see -- per-stripe
send/ack round-trip time, the AIMD's chosen capture rate, the CRF/window servo
state, and the three kinds of frame drop -- and then throws the raw values away.
This module is the "dumb sink": the hot paths ``emit(browser_id, record)`` a small
dict into a per-browser ring, and a separate read-only WebSocket (the firehose,
wired in ``runner``) drains it to a standalone client-side lens that does ALL the
joining, percentiles and drawing. Nothing here is ever read back into a control
decision, so recording cannot change what the pipe does.

Watch-only discipline (the constraint the whole design rests on):

* ``emit`` never takes a lock and never blocks. It reads two atomic references
  (``dict.get`` + an immutable subscriber tuple) and does ``deque.append`` (all
  GIL-atomic), so it can run inside the pipe's ``_condition`` critical sections
  without lengthening them meaningfully and without introducing a new lock that a
  slow lens could back-pressure through.
* The subscriber tuple is swapped wholesale on (un)subscribe under ``_lock`` -- a
  rare event -- so ``emit`` iterating it is always safe (it sees the old or the new
  tuple, both valid) and never contends that lock.
* Every record carries a per-browser ``seq``; a gap the lens sees means the ring
  overflowed and it MISSED data -- the tool admits its own blind spots rather than
  drawing a clean-but-false picture.
* ``emit`` swallows everything: a telemetry bug must never take down the stream.

Volume is tiny (this host has 2 cores -> pixelflux runs 2 stripe rows -> ~360
records/s), so a per-browser history ring plus per-subscriber queues costs a few MB
and no measurable CPU.
"""

import itertools
import socket as socket_module
import struct
import threading
import time
from collections import deque
from typing import Any

from loguru import logger

# Late-join replay depth: a lens attaching mid-session gets this many recent
# records so its figures aren't blank until fresh data trickles in (~11s at 360/s).
_HISTORY_MAXLEN = 4000
# Per-subscriber queue depth: a slow lens drops OLDEST here (deque maxlen) rather
# than back-pressuring the pipe; the seq gap makes the loss visible in the UI.
_SUBSCRIBER_MAXLEN = 20000


class _BrowserTelemetry:
    """One browser's recording state: the replay history, the live subscriber
    queues (an immutable tuple, swapped on change), and a per-browser sequence
    counter so the lens can detect ring overflow as a seq gap."""

    def __init__(self) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_MAXLEN)
        self.subscribers: tuple[deque[dict[str, Any]], ...] = ()
        self.seq = itertools.count()
        self.refs = 0  # open stream connections (ref-counted for split-view)


class TelemetryHub:
    """Per-browser fan-out sink. ``open``/``close`` bracket a stream connection;
    ``emit`` is the lock-free hot-path append; ``subscribe``/``unsubscribe`` serve
    the read-only firehose."""

    def __init__(self) -> None:
        self._state: dict[str, _BrowserTelemetry] = {}
        self._lock = threading.Lock()

    def _gc_locked(self, browser_id: str, state: "_BrowserTelemetry") -> None:
        """Drop a browser's state once nothing needs it (no open streams AND no
        subscribed lenses). Caller holds ``_lock``."""
        if state.refs <= 0 and not state.subscribers:
            self._state.pop(browser_id, None)

    def open(self, browser_id: str) -> None:
        """Begin recording for a browser stream connection (ref-counted, so a
        second split-view viewer doesn't wipe the first, and a lens that subscribed
        first keeps its queue). Creates state only if absent."""
        with self._lock:
            state = self._state.get(browser_id)
            if state is None:
                state = _BrowserTelemetry()
                self._state[browser_id] = state
            state.refs += 1

    def close(self, browser_id: str) -> None:
        with self._lock:
            state = self._state.get(browser_id)
            if state is None:
                return
            state.refs -= 1
            self._gc_locked(browser_id, state)

    def emit(self, browser_id: str, record: dict[str, Any]) -> None:
        """Append a record to the browser's history and every live subscriber.

        Lock-free and non-blocking: ``dict.get`` and the tuple read are atomic, and
        ``deque.append`` is atomic; a full subscriber queue drops its oldest. Wrapped
        so a telemetry defect can never propagate into the stream's hot path."""
        try:
            state = self._state.get(browser_id)
            if state is None:
                return
            record["seq"] = next(state.seq)
            record["t"] = time.monotonic()
            state.history.append(record)
            for queue in state.subscribers:
                queue.append(record)
        except Exception as error:  # noqa: BLE001  (telemetry must never break the stream)
            logger.debug("telemetry emit dropped a record ({})", error)

    def subscribe(self, browser_id: str) -> tuple[list[dict[str, Any]], deque[dict[str, Any]]]:
        """Register a firehose consumer: returns (history snapshot, live queue)."""
        queue: deque[dict[str, Any]] = deque(maxlen=_SUBSCRIBER_MAXLEN)
        with self._lock:
            state = self._state.get(browser_id)
            if state is None:
                # Lens attached before (or between) stream connections: create the state
                # and keep it alive via this subscriber until open() ref-counts it. open()
                # only creates-if-absent, so it will NOT drop this subscriber.
                state = _BrowserTelemetry()
                self._state[browser_id] = state
            history = list(state.history)
            state.subscribers = state.subscribers + (queue,)
        return history, queue

    def unsubscribe(self, browser_id: str, queue: deque[dict[str, Any]]) -> None:
        with self._lock:
            state = self._state.get(browser_id)
            if state is None:
                return
            state.subscribers = tuple(q for q in state.subscribers if q is not queue)
            self._gc_locked(browser_id, state)


# Process-wide singleton; the pipe, the sender loop and the firehose all reach it here.
hub = TelemetryHub()


# --- TCP_INFO (the local-hop sanity check) -----------------------------------
# The daemon's stream socket peers with a LOCAL forwarder (it binds 127.0.0.1),
# so this reflects the loopback hop, NOT the WAN path to the viewer. It cannot see
# east-coast packet loss (that lives on the downstream tunnel); it only confirms the
# local segment is clean, ruling out local buffering as a cause. Verified readable
# under gVisor (full 224-byte struct, live rtt/cwnd).
_TCP_INFO = 11  # SOL_TCP / IPPROTO_TCP TCP_INFO on Linux


def read_tcp_info(sock: Any) -> dict[str, int] | None:
    """Parse a subset of ``struct tcp_info`` from the socket, or None if unavailable.

    Layout: 8 leading u8 fields, then a u32 array (little-endian). Field indices
    verified against the live kernel/gVisor struct."""
    try:
        raw = sock.getsockopt(socket_module.IPPROTO_TCP, _TCP_INFO, 256)
    except OSError:
        return None
    if len(raw) < 8:
        return None

    def u32(index: int) -> int | None:
        offset = 8 + index * 4
        if offset + 4 > len(raw):
            return None
        return struct.unpack_from("<I", raw, offset)[0]

    return {
        "ca_state": raw[1],
        "retransmits": raw[2],
        "unacked": u32(4) or 0,
        "lost": u32(6) or 0,
        "retrans": u32(7) or 0,
        "rtt_us": u32(15) or 0,
        "rttvar_us": u32(16) or 0,
        "snd_cwnd": u32(18) or 0,
        "total_retrans": u32(23) or 0,
    }
