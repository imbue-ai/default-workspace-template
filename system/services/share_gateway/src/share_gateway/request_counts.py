"""Per-minute counts of the requests the gateway verified, the ``gateway_requests`` event feed.

Every request caddy asks ``/_auth/verify`` about is counted once, under the key the handler settles by the time it
answers: the app the Host resolved to, who asked (the owner, a visitor's user id, a visiting agent's id, or nobody),
whether the request read, wrote or opened a WebSocket, and whether it was allowed or why it was denied. The counts
are kept in memory and written once a minute to ``$MNGR_AGENT_STATE_DIR/events/gateway_requests/events.jsonl``,
one record per key per minute, under the standard event envelope, which is what the analytics collection reads
from an explorer-plan workspace. Nothing per request touches the filesystem, and nothing here ever records a path,
a query string, an address or a header: caddy's access log (``caddyfile.py``) is the owner's per-request record.
"""

import json
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

from event_streams.archive import compress_staged_streams
from event_streams.archive import stage_oversized_stream
from event_streams.archive import staged_streams

from share_gateway import agent_events
from share_gateway.agent_events import EVENTS_FILENAME
from share_gateway.log import log

EVENT_SOURCE: Final[str] = "gateway_requests"
EVENT_TYPE: Final[str] = "gateway_request_count"
WINDOW_SECONDS: Final[int] = 60
FLUSH_INTERVAL_SECONDS: Final[float] = 60.0
# The most keys one window keeps; every further key of the window folds into ``OVERFLOW_REQUESTER_KIND``.
MAX_KEYS_PER_WINDOW: Final[int] = 256
ARCHIVE_THRESHOLD_BYTES: Final[int] = 32 * 1024 * 1024
KEPT_ARCHIVE_COUNT: Final[int] = 2

REQUESTER_KIND_OWNER: Final[str] = "owner"
REQUESTER_KIND_USER: Final[str] = "user"
REQUESTER_KIND_AGENT: Final[str] = "agent"
REQUESTER_KIND_NONE: Final[str] = "none"
OVERFLOW_REQUESTER_KIND: Final[str] = "overflow"

METHOD_CLASS_READ: Final[str] = "read"
METHOD_CLASS_WRITE: Final[str] = "write"
METHOD_CLASS_WEBSOCKET: Final[str] = "websocket"
_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

OUTCOME_ALLOWED: Final[str] = "allowed"
# One denial reason per refusal the verify handler can give, spelled ``denied:<reason>`` in the record.
DENIED_FOREIGN_HOST: Final[str] = "denied:foreign_host"
DENIED_ORIGIN: Final[str] = "denied:origin"
DENIED_AGENT_ACCESS_UNCONFIGURED: Final[str] = "denied:agent_access_unconfigured"
DENIED_AGENT_TOKEN: Final[str] = "denied:agent_token"
DENIED_NO_SESSION: Final[str] = "denied:no_session"
DENIED_GRANTS_MISCONFIGURED: Final[str] = "denied:grants_misconfigured"
DENIED_NO_GRANT: Final[str] = "denied:no_grant"

# (service, requester_kind, requester_id, method_class, outcome)
RequestKey = tuple[str, str, str, str, str]
_OVERFLOW_KEY: Final[RequestKey] = ("", OVERFLOW_REQUESTER_KIND, "", "", OUTCOME_ALLOWED)


def events_path_from_environ(environ: Mapping[str, str]) -> Path | None:
    """Where the feed is written, or None (and no counting) when the services agent's state dir is unset."""
    events_dir = agent_events.events_dir_from_environ(environ, EVENT_SOURCE)
    if events_dir is None:
        return None
    return events_dir / EVENTS_FILENAME


def classify_method(method: str, is_websocket_upgrade: bool) -> str:
    if is_websocket_upgrade:
        return METHOD_CLASS_WEBSOCKET
    return METHOD_CLASS_READ if method.upper() in _READ_METHODS else METHOD_CLASS_WRITE


def window_start_of(now: float) -> int:
    """The epoch second the one-minute window holding ``now`` starts at."""
    return int(now) - int(now) % WINDOW_SECONDS


def _nanosecond_iso(epoch_seconds: int) -> str:
    base = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.000000000Z"


def _window_start_iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def render_count_record(window_start: int, key: RequestKey, request_count: int) -> dict[str, object]:
    """One feed record: the window, the key's five fields, and the count, under the standard envelope."""
    service, requester_kind, requester_id, method_class, outcome = key
    return {
        # The window's end: when the count became final.
        "timestamp": _nanosecond_iso(window_start + WINDOW_SECONDS),
        "type": EVENT_TYPE,
        "event_id": f"evt-{uuid4().hex}",
        "source": EVENT_SOURCE,
        "window_start": _window_start_iso(window_start),
        "window_seconds": WINDOW_SECONDS,
        "service": service,
        "requester_kind": requester_kind,
        "requester_id": requester_id,
        "method_class": method_class,
        "outcome": outcome,
        "request_count": request_count,
    }


class RequestCounter:
    """The in-memory counts of one share stack, keyed by window and request key, behind one lock."""

    def __init__(self, events_path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self.events_path = events_path
        self._clock = clock
        self._lock = threading.Lock()
        self._count_by_window_and_key: dict[tuple[int, RequestKey], int] = {}
        self._key_count_by_window: dict[int, int] = {}
        self._is_disabled_logged = False

    def record(self, service: str, requester_kind: str, requester_id: str, method_class: str, outcome: str) -> None:
        """Count one verified request; never raises and never touches the filesystem."""
        if self.events_path is None:
            if not self._is_disabled_logged:
                log("MNGR_AGENT_STATE_DIR unset; not counting gateway requests")
                self._is_disabled_logged = True
            return
        window_start = window_start_of(self._clock())
        key: RequestKey = (service, requester_kind, requester_id, method_class, outcome)
        with self._lock:
            if (window_start, key) not in self._count_by_window_and_key:
                if self._key_count_by_window.get(window_start, 0) >= MAX_KEYS_PER_WINDOW:
                    key = _OVERFLOW_KEY
                else:
                    self._key_count_by_window[window_start] = self._key_count_by_window.get(window_start, 0) + 1
            self._count_by_window_and_key[(window_start, key)] = (
                self._count_by_window_and_key.get((window_start, key), 0) + 1
            )

    def drain(self, is_final: bool) -> list[dict[str, object]]:
        """Take every finished window's counts out as records, or every window's when ``is_final``."""
        current_window_start = window_start_of(self._clock())
        with self._lock:
            drained = [
                (window_start, key, count)
                for (window_start, key), count in self._count_by_window_and_key.items()
                if is_final or window_start < current_window_start
            ]
            for window_start, key, _count in drained:
                del self._count_by_window_and_key[(window_start, key)]
            for window_start in {window_start for window_start, _key, _count in drained}:
                self._key_count_by_window.pop(window_start, None)
        drained.sort(key=lambda entry: (entry[0], entry[1]))
        return [render_count_record(window_start, key, count) for window_start, key, count in drained]

    def flush(self, is_final: bool) -> None:
        """Append every finished window's records to the feed; a failed write is logged and the records dropped."""
        if self.events_path is None:
            return
        records = self.drain(is_final)
        if not records:
            return
        try:
            self._append(records)
        except OSError as exc:
            log(f"Could not write {len(records)} gateway request counts to {self.events_path}: {exc}")

    def _append(self, records: list[dict[str, object]]) -> None:
        events_path = self.events_path
        if events_path is None:
            return
        events_path.parent.mkdir(parents=True, exist_ok=True)
        is_staged = stage_oversized_stream(events_path, ARCHIVE_THRESHOLD_BYTES)
        with events_path.open("a", encoding="utf-8") as events_file:
            for record in records:
                events_file.write(json.dumps(record, sort_keys=True) + "\n")
        # Appends run on the flusher thread (or the runner's stop path), never a request thread, so the staged
        # stream is gzipped right here once the new stream exists. A stream a gateway staged but died before
        # compressing is finished here too, rather than waiting for the next stream to grow past the threshold.
        if is_staged or staged_streams(events_path):
            try:
                compress_staged_streams(events_path, KEPT_ARCHIVE_COUNT)
            except OSError as exc:
                log(f"Could not archive the staged gateway request counts beside {events_path}: {exc}")


class RequestCountFlusher:
    """The thread that flushes a counter's finished windows once a minute, and everything when stopped."""

    def __init__(self, counter: RequestCounter, interval_seconds: float = FLUSH_INTERVAL_SECONDS) -> None:
        self._counter = counter
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="share-gateway-request-counts", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread and write every window, the current one included."""
        self._stop_event.set()
        self._thread.join(timeout=self._interval_seconds)
        self._counter.flush(is_final=True)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            self._counter.flush(is_final=False)
